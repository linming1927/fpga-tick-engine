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

**v3.22** — real historical market data replay against real hardware,
the bring-up step promised (but not yet built) when --selftest passed.
New --source historical on order_manager.py: replays REAL trades from
fetch_historical_trades.py's JSONL (the exact files backtest.py scores
offline) through the actual board, verified the same bit-for-bit way
as every other signal here — --source sim's random walk can only ever
prove the math works on synthetic data; this proves it on the actual
price/volume patterns the strategy will trade on. Single-symbol only
(one trades file = one symbol, backtest.py's own convention);
--replay-rate paces the replay (real recorded inter-tick gaps are NOT
reproduced — files can span hundreds of millions of trades, replaying
verbatim gaps would take hours per trading day) and --replay-max caps
how many trades a bring-up run touches. Safety guard: --live combined
with --source historical is now a hard refusal with a clear reason —
replaying already-happened data through a real broker would place
real trades on stale prices; this gap existed before this drop and is
closed by it. Refactor along the way: iter_trades/iter_trades_multi
moved from backtest.py to tick_protocol.py (the shared, dependency-
free base — moving them into bridge.py directly would have created a
circular import via bridge -> backtest -> order_manager -> bridge), so
offline backtesting and live hardware replay now share ONE exact file
reader instead of two that could quietly diverge; backtest.py imports
them from their new home, zero behavioral change (test_backtest.py
unchanged, 71/71). New [G20] in test_order_manager.py: CLI wiring, the
--live safety refusal, missing---trades refusal, multi-symbol refusal,
and an end-to-end replay against the emulator with a real-shaped
synthetic trades file — all three engines verified, zero divergence.
16 new checks; 713 total across the host suite, 0 failures.

**v3.23** — VWAP can now be the LIVE trading strategy. New
--strategy vwap_bounce trades the FABRIC-verified VWAP engine's
signals (wire 0x85) for real, with SMA and EMA both scored in the
background as peers — the same role EMA has always played alongside
a live SMA, now with three strategies instead of two. This retired
the special-case machinery VWAP-FPGA needed when it could only ever
be scored (a per-symbol _vwap_fpga_card lazily created outside the
normal labels/cards system, with dedicated branches in on_verified
and route_to_shadow_cards) in favor of making vwap_bounce a genuine
third peer sharing the exact same architecture SMA/EMA already used:
one shared card (positions keyed by symbol internally, same as
SMA/EMA), created unconditionally alongside them, live iff selected.
Generalizing beat special-casing further. Also fixed a real latent
bug this surfaced: the startup replay that restores scored strategies'
trips/wins/net$ across a restart used a hardcoded "ema if sma else
sma" binary swap for "the other strategy" — correct with exactly two
peers, silently wrong with three (it would restore only ONE of the
two non-live strategies, dropping the other's history every restart).
Now generalized to restore every scored strategy, whichever is live.
Clear separation preserved in the help text and the label itself
between this (fabric-verified, tradeable) and the pre-existing
--vwap-bounce flag (host-computed, always score-only, usable
alongside any --strategy choice for comparison). New [G21] in
test_order_manager.py: --strategy vwap_bounce end-to-end against the
emulator (VWAP-FPGA [LIVE], SMA and EMA both present and NOT live,
a real fill, zero divergence across all three), and a restart
simulation confirming the generalized replay restores both non-live
strategies by name, not just one. 9 new checks; 722 total across the
host suite, 0 failures.

**v3.24** — a real gap found while reviewing v3.23 for real-money
readiness, fixed before it mattered: the live-trading confirmation
banner never showed WHICH STRATEGY was about to place real orders —
only the symbol. With --strategy now a 3-way choice (v3.23), a flag
typo or a stale saved command could arm live trading against a
completely different strategy than intended, and the operator would
have no way to catch it from the banner or the confirmation prompt.
arm_live_trading() now takes a REQUIRED strategy parameter (no
default — every caller must be explicit), prints it plainly in the
banner ("strategy VWAP_BOUNCE <-- this one TRADES; the others are
scored only"), and requires it in the retyped confirmation phrase
("LIVE SPY VWAP_BOUNCE", not just "LIVE SPY") — the same two-key
discipline this banner already used for the symbol, extended to
cover the dimension that just became variable. New checks in
test_live_gating.py: the old symbol-only phrase now correctly
refuses, a mismatched strategy name in the confirmation refuses (the
exact mistake this exists to catch), a correctly matching phrase
still arms, and the banner text itself is checked (not just the
gate's return value) to confirm an operator reading it before typing
anything already sees which strategy is about to trade. 7 new
checks; 729 total across the host suite, 0 failures.

**v3.25** — README.md caught up with everything since v2.0 (which is
where it had stopped — none of the profit-gated/blend/dashboard/VWAP
work in v3.12-v3.24 was documented there). Fixed two now-inaccurate
examples rather than leave them misleading: the live-arming command
and confirmation phrase (was "LIVE SPY", now must include the
strategy per v3.24), and the "run the whole system" section now
points to the newer, more complete examples instead of stopping at
basic paper trading. New "VWAP FPGA Engine & Live Strategy Selection"
section: what the RTL/host verification chain actually is, the
--selftest hardware acceptance test command, the --source historical
replay command (with the flags that matter: --trades, --replay-rate,
--replay-max, the --live refusal), and --strategy vwap_bounce for
making VWAP the live strategy — plus an explicit reminder of the
pre-live gate (clean historical replay, zero divergence, before
--live). Final test census corrected to the current 4846 (4117 RTL +
729 host); the old count was 6+ drops stale. Documentation only, no
code changes — no new checks, host suite unchanged at 729.

**v3.26** — full order_manager.py CLI reference added to README.md:
every flag (~40), grouped by what it actually controls (connection,
bitstream-matching params, strategy selection, scored-only comparisons,
data source, broker/risk limits, verification tuning, logging, cost/
tax estimate, monitoring/diagnostics), with a short accurate description
of each — including several that had no --help text at all (--port,
--fast/--slow, --n/--rate/--start-price, --broker, --qty, --max-shares,
--max-notional, --max-orders-per-day, --cooldown, --audit,
--filing-status), written from reading the actual enforcement code
rather than guessed. Caught and fixed two inaccuracies before they
went in: --ignore-market-hours isn't refused when --live is set, it's
silently overridden (require_market_hours is forced on unconditionally
in live mode regardless of the flag) — a meaningfully different and
more useful thing to know than "cannot be set"; and the cost/tax
estimate flags work in any mode, it's specifically the resulting fee/
tax figures that are real vs. hypothetical depending on paper or live,
not the flags' applicability. Documentation only, no code changes —
host suite unchanged at 729.

**v3.27** — order_manager.py's buy sizing moved from share-count to
dollar-exposure, per request: --qty default 1->5 shares per buy
signal, --max-notional default $2,000->$3,000 (unchanged mechanism,
still a per-order cap), and --max-shares REMOVED entirely from the
CLI, replaced by --max-position-notional (default $10,000) — a cap
on the TOTAL position's dollar value (existing holdings + this buy,
at current price), not share count. Deliberately global (applies to
whichever strategy is live — sma/ema/vwap_bounce — matching how
every other risk limit already works, not special-cased to VWAP).
Real landmine found and avoided before implementing: RiskLimits.
max_shares is NOT order_manager.py's private field — blended_
strategy.py's per-sleeve capital allocation (vwap_shares=6, pg_
shares=4 from way back) and backtest.py's own --max-shares flag both
depend on that exact mechanism continuing to work. A literal deletion
would have silently broken the blend strategy's tuned sleeve
allocation. Fix: the new max_position_notional_e4 field is ADDITIVE
and opt-in (None by default, a true no-op) — every existing consumer
of the shared dataclass is completely unaffected unless it explicitly
sets the field, which only order_manager.py's own CLI now does; the
old max_shares check in RiskPolicy.evaluate() is completely untouched
and still independently enforced. From order_manager.py's CLI
surface, --max-shares is fully gone (now fails argparse outright,
loud not silent, if anyone passes it); underneath, nothing else
broke. Verified at three levels: unit tests proving the new check
blocks/allows correctly AND that the old max_shares mechanism still
independently blocks even when the new dollar check would have
allowed it; a CLI-level test confirming the old flag now errors;
and an end-to-end run against the emulator confirming a real fill
actually sizes at the new 5-share default. Fixed a test that was
asserting backtest.py's and order_manager.py's CLI defaults stay
identical — a premise now deliberately false for --max-shares/--max-
notional specifically (backtest.py intentionally unchanged; offline
analysis has no reason to inherit a live-trading-specific sizing
preference) — replaced with explicit checks of each tool's own
correct values instead of silently loosening the comparison. README
updated: the CLI reference table, both live-trading examples, and
the backtest-defaults-parity claim that this drop made false. 15 new
checks; 744 total across the host suite, 0 failures.

**v3.28** — --relay-url: connect through a local relay instead of
directly to Alpaca, for running this project alongside another one
(the separate ladder-trader project) that needs the same live price
feed at the same time — Alpaca allows only one direct websocket
connection per login, even on paid tiers. Point this at a running
alpaca_relay.py (e.g. ws://localhost:8765) and everything else is
unchanged; omit it and behavior is identical to before this existed.
Delivered from two uploaded files (order_manager.py, bridge.py) that
turned out to be based on a pre-v3.27 snapshot — applying them
verbatim would have silently reverted the just-shipped position-
sizing work (--max-position-notional, the $10k dollar-exposure cap,
the qty/notional default changes). Diffed against the current tree
first, isolated the one genuinely new capability (relay_url threading
through run_alpaca, in both bridge.py's own small CLI and
order_manager.py's primary one), and applied only that on top of the
current, up-to-date codebase — v3.27 is untouched and confirmed
intact (--max-position-notional still present, same defaults). New
[G1c] in test_order_manager.py verifies the actual connection target
by intercepting the real WebSocketApp construction (a fake websocket
module swapped into sys.modules, zero real network I/O) rather than
just checking the CLI flag exists: relay_url set routes to that URL
exactly, relay_url unset (the default) still resolves to the real
Alpaca endpoint unchanged. 2 new checks; 746 total across the host
suite, 0 failures.

**v3.29** — this project can now run entirely without an FPGA board,
per request (freeing the Arty for another project). fpga_emulator.py
was already a pure-Python, bit-exact stand-in for the real board (it
computes every signal through the same SMAMirror/EMAMirror/VWAPMirror
classes every hardware signal is verified against) but had only ever
been used as an internal test fixture — imported, run for a few
hundred ticks, torn down. Made it a genuine, documented, first-class
replacement for a real board instead of rearchitecting the signal
path to bypass hardware verification (which would have meant giving
up the entire verified-against-an-independent-model discipline this
project is built on, for no reason — the emulator already IS that
model). Extended: constructor and CLI gained --ema-kf/--ema-ks/
--vwap-warmup/--vwap-k2-q8 for full parameter parity with
order_manager.py's own bitstream-matching flags (previously hardcoded
to defaults, no way to override); new --port-symlink (default
/tmp/fpga-tick-emulator) maintains a STABLE path across restarts,
since a bare pty's path changes every run — the same role a real
board's /dev/ttyUSBx plays, without the USB enumeration quirks a real
board has. Found and fixed a real gap while building this: cleanup
(symlink removal) only ran on Ctrl-C/SIGINT; a bare `kill <pid>`
(SIGTERM, the default signal, and the more likely one from a process
manager or a closed terminal) didn't trigger it at all, leaving a
stale dangling symlink. Added a SIGTERM handler routing through the
same cleanup path. Verified at every level: unit tests that the new
constructor params actually reach the VWAPMirror instances (not just
accepted and dropped); a real subprocess test sending real serial
traffic through the symlink path via pyserial (not the raw pty) and
confirming a genuine 0x90 ack comes back; confirmed clean shutdown on
BOTH signals; confirmed a stale symlink or an unrelated plain file
already sitting at the target path gets replaced without erroring.
Beyond the unit level: ran a full ~40-second, 6000-tick, 5-symbol
session (SPY/QQQ/RKLB/TSLA/RIVN) through order_manager.py with
--strategy vwap_bounce and --dashboard live, connected entirely
through the symlink with zero real hardware anywhere — dashboard
reachable mid-session with correct multi-symbol data, all three
strategies verified with zero divergence throughout, real fills
placed on the live VWAP strategy. New test_fpga_emulator.py (19
checks) is the first dedicated coverage of the emulator's own
standalone CLI, as opposed to its long-standing use as an imported
test fixture. README: new "Running without an FPGA board" section
documenting this as a first-class workflow, not a footnote. 19 new
checks; 765 total across the host suite, 0 failures.

**v3.30** — fixed a real macOS-only bug found running order_manager.py
against fpga_emulator.py on a Mac for the first time: connecting to
the emulator's pty threw OSError: [Errno 25] Inappropriate ioctl for
device inside pyserial's macOS backend. Root cause: order_manager.py's
--baud defaults to 921600, a non-standard rate that macOS's termios
module has no native constant for; pyserial falls through to a
special IOSSIOSPEED ioctl to set it, which only real serial hardware
supports — a pty has no actual UART underneath it, so macOS correctly
refuses the ioctl with ENOTTY. Linux's pty implementation is more
permissive (accepts the call even though it's meaningless for a pty),
which is exactly why this never surfaced there. Baud rate has zero
effect on a pty's actual behavior either way -- no real clock involved,
bytes move at whatever rate the reader consumes them -- so bridge.py
now catches this specific failure (errno 25, requested baud wasn't
already 115200) and transparently retries at 115200, a rate every
platform's termios supports natively. An unrelated OSError (different
errno -- a genuine hardware/driver problem) is never swallowed by
this and propagates normally, confirmed directly. Verified without a
real Mac available to test on: mocked pyserial's exact macOS failure
mode (a bare OSError(errno=25) on the first call) and confirmed the
retry sequence tries the requested baud first, falls back to exactly
115200 second, and stops there -- no retry storm, no silent
swallowing of unrelated failures, no pointless retry-at-the-same-value
when 115200 itself is what failed. 6 new checks; 771 total across the
host suite, 0 failures.

**v3.31** — fixed a real macOS-only hang, found running the test
suite on a Mac for the first time: fpga_emulator.py's background
reader thread could get stuck forever in an uninterruptible kernel
wait, immune to Ctrl-C and requiring SIGKILL. Root cause: the
original stop() only set a flag and closed the pty's file
descriptors, entirely trusting that closing an fd from the main
thread would reliably interrupt a bare blocking os.read() happening
concurrently in the reader thread. That's platform-inconsistent --
Linux's pty implementation mostly wakes the blocked reader when the
fd closes elsewhere; macOS/BSD kernels are documented to sometimes
leave it blocked instead, which is exactly what `ps` showed (state
"U", uninterruptible -- not even a normal signal reaches a thread in
that state, only the read completing or the process being killed
outright). Fixed by removing the dependency on that race entirely
rather than trying to make close() reliable: the reader loop now
uses select() with a 0.2s timeout before each read, so it re-checks
its own stop flag on a fixed schedule no matter what happens to the
fd from another thread. stop() also now joins the thread (bounded at
2s) instead of returning immediately regardless of whether the
thread actually exited -- which is exactly the gap that let the
original bug hide silently: the old stop() "succeeded" instantly
every time on Linux specifically because Linux happened to make the
race work out, with nothing to ever catch it not doing so elsewhere.
New [G6] in test_fpga_emulator.py: confirms stop() is now genuinely
synchronous (thread.is_alive() is False immediately after stop()
returns, not just "probably soon"), tested against the reader
sitting genuinely idle in its wait (the actual condition that hung)
and against stopping immediately after start (the race's other
edge). 5 new checks; 776 total across the host suite, 0 failures.

**v3.32** — fixed a leftover instance of the exact bug v3.30 fixed,
just in test infrastructure instead of production code: found running
test_fpga_emulator.py on a Mac for the first time (v3.29 was written
and only ever tested on Linux, before v3.30 discovered this class of
bug at all). G3's real-serial-traffic check connects directly to the
pty (deliberately bypassing Bridge.__init__, since it's testing the
symlink itself) and had hardcoded baud=921600 -- the exact
non-standard rate that needs macOS's special IOSSIOSPEED ioctl, which
a pty can't satisfy (ENOTTY), and which this raw connection never
goes through Bridge to get v3.30's fallback for. Fixed by using a
standard rate (115200) directly -- baud is meaningless for a pty's
actual behavior either way, so there was never a reason for this
specific line to use a non-standard one. Grepped the entire codebase
for every serial.Serial() call site to confirm this was the only
remaining instance; bridge.py's two call sites were already correct.
No new checks needed (this is a fix to an existing check's internal
value, not new behavior to verify) -- 776 total across the host
suite, 0 failures, unchanged.

**v3.33** — fixed another Linux-specific assumption in test
infrastructure, found running on a Mac: two checks in
test_fpga_emulator.py asserted the emulator's pty path starts with
"/dev/pts/" -- Linux's pty naming convention. macOS names pty slaves
/dev/ttysNNN instead (visible in the user's own earlier session:
"virtual FPGA listening on: /dev/ttys002"), so both checks failed on
a real Mac despite the underlying mechanism working correctly (the
symlink was pointing at a genuine, working pty the whole time -- only
the assertion's hardcoded prefix was wrong). Replaced with a
platform-agnostic check (_is_real_pty_target): verifies the target is
an existing character-special device under /dev/, true on both
platforms, rather than hardcoding either OS's specific subdirectory
naming. Also fixed two documentation-only stale references found
while in the area: fpga_emulator.py's docstring now mentions both
platforms' pty path conventions instead of only Linux's, and
order_manager.py's own module docstring had a genuinely stale
--max-shares example (removed from this CLI entirely back in v3.27,
replaced by --max-position-notional) that would have failed if
copy-pasted -- fixed to the current flag. Grepped for every
--max-shares reference across the whole codebase to confirm nothing
else was missed; everything remaining is either backtest.py's own
still-valid flag or test code correctly verifying its absence from
order_manager.py. No new checks needed (existing checks fixed to
verify correctly, not new behavior added) -- 776 total across the
host suite, 0 failures, unchanged.

**v3.34** — README's "Running without an FPGA board" section updated
with everything learned actually running the two-terminal workflow
day to day: the real working command (matching the flags actually
used in practice: --baud, --source alpaca, --broker alpaca,
--household-income, --log, --audit), an explicit macOS note about
--baud 115200 (v3.30's fallback catches it automatically, but stating
it avoids the warning), a correction that --source alpaca must be
stated explicitly since --source defaults to sim not alpaca (the
exact mistake made once already -- an omitted --source silently runs
a fake session instead of connecting to real market data, with no
error to catch it), a note that --symbol on the emulator is only a
startup placeholder fully overwritten by order_manager.py's own
--symbols on connect, what a healthy session's output actually looks
like (verified.../RESULT: OK vs DIVERGENCE), and an explicit warning
about the actual failure mode when terminal 1 isn't running (opens
successfully, zero ticks, clean exit, no error -- easy to mistake for
something else being wrong). Documentation only, no code changes --
host suite unchanged at 776.

**v3.35** — fixed the real bug behind "the session just ends itself":
a SystemExit from deep inside run_sim/run_historical/run_alpaca
(missing credentials, missing dependency, a failed slot-configuration
handshake, etc.) was being silently swallowed by main()'s own finally
block, which unconditionally calls its own sys.exit() at the very
end -- overriding whatever exception was already propagating from the
try block, real error message included. The result looked exactly
like a normal, empty, successful session: a complete-looking summary,
all-zero counters, RESULT: OK, exit code 0 -- with zero indication
anywhere of what actually happened or why. Root cause found by tracing
through a real user session line by line: confirmed configure_symbols()
always prints one of exactly two messages on completion (never both
silent), neither appeared, meaning run_alpaca() exited before even
reaching that call -- pointing straight at one of its own early
sys.exit() calls (missing ALPACA_KEY/SECRET, missing the
websocket-client dependency) being swallowed. Fixed by catching
SystemExit explicitly alongside the existing KeyboardInterrupt,
printing the real reason ("[om] session aborted early: <message>")
before the finally block's own summary/exit logic runs, and tracking
that an early abort happened so the final exit code honestly reflects
failure instead of reporting success for a session that never started.
Verified against the real failure path end to end (missing Alpaca
credentials through the actual CLI, not a synthetic mock) -- confirmed
the real reason now prints, the exit code is nonzero, and a normal
successful session is completely unaffected (same exit-code logic,
no spurious message). 6 new checks; 782 total across the host suite,
0 failures.
