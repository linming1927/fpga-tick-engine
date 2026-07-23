# FPGA Tick Trading Engine

A Digilent Arty A7-100T computes SMA + EMA crossover signals in fabric from
real-time market ticks, streamed over UART from a Python host bridge to
Alpaca (live IEX data, paper-trading orders). Runtime-configurable symbol
slots (up to 8, any S&P 500 ticker), a risk-gated order manager, and a web
console. See CHANGELOG.md for the full drop-by-drop history.

## Quick Start

**1. Start the whole system (bridge + risk-gated orders + web console):**
```bash
python3 host/order_manager.py --port /dev/ttyUSB1 --source alpaca --broker alpaca \
    --symbols SPY,QQQ --strategy sma --dashboard 8000 \
    --household-income 185000 --log ticks.jsonl --audit audit.jsonl
```
- `--symbols` — up to 8, comma-separated, any S&P 500 ticker (GOOGL, BRK.B, ...)
- `--strategy sma|ema` — which one actually trades; the other is scored
  through an identical risk-gated replay for a fair comparison
- `--broker mock` instead of `alpaca` to paper-trade against a local mock
  broker with no network calls at all (still uses real Alpaca market data)
- `--source sim` instead of `alpaca` to drive it with a synthetic random
  walk — no market hours, no network, no keys required

**2. Open the GUI** (only exists once step 1 is running, since the console
is served by the order-manager process itself):
```
http://localhost:8000
```
From your phone on the same LAN: `http://<this-machine's-LAN-IP>:8000`.

**No hardware handy?** Run the virtual board first, then point the same
command at it:
```bash
python3 host/fpga_emulator.py --symbol "SPY " --fast 8 --slow 32
#   -> prints: virtual FPGA listening on: /dev/pts/N
python3 host/order_manager.py --port /dev/pts/N --source sim --broker mock \
    --cooldown 0 --n 300 --rate 20 --dashboard 8000
```

**Position sizing (v3.27):** buys accumulate up to `--max-position-notional`
(default $10,000 total exposure) per symbol — a signal to buy while already
holding is allowed as long as the RESULTING position's dollar value stays
under the cap, not refused outright. Sized by dollar exposure, not share
count — the older share-count cap (`max_shares`) still exists underneath
for `backtest.py` and the blend strategy's own per-sleeve allocation, but
`order_manager.py`'s live/paper CLI no longer exposes or is meaningfully
constrained by it. A single SELL always closes the *entire* accumulated
position at once,
correctly priced against the weighted-average cost basis across
however many buys built it up. Cooldown and the daily order cap still
gate every individual order regardless of position size, so this isn't
unthrottled — it's deliberately closer to a scaling-in strategy than a
strict one-lot-at-a-time system.

**Signal verification (`--verify-grace-s`, default 2.0):** every FPGA
signal must be independently confirmed by the host's own model
computation before it becomes a real order — that's what the "verified"
in "verified FPGA signal" means. If they don't line up within
`--verify-grace-s` REAL SECONDS, the kill switch trips with
`"model/hardware divergence: orphan ... signal"`. This is deliberately
measured in real elapsed time, not an echo count — an earlier version
used a fixed count of echoes, which has no fixed real-time meaning:
during a burst (multiple symbols firing at once, the daily order cap
already maxed out, signals piling up with nothing to absorb them), a
small echo count is consumed almost instantly, giving very little real
tolerance exactly when timing pressure is highest. If this divergence
recurs and you can rule out a genuine hardware fault (check the audit
log's `KILL` event — it now carries the symbol, strategy, how long it
waited, and the actual signal contents that didn't match, not just the
one-line reason), raising `--verify-grace-s` is the first thing to try
before assuming something is actually broken.

**The GUI's signals table shows what actually happened to each
signal** — a new outcome column reads `FILLED`, `blocked: <reason>`
(cooldown, daily cap, position ceiling, market closed), `rejected:
<broker error>`, or `ignored: <reason>` for the untraded/scored
strategy's own housekeeping (already open, ladder full, nothing to
sell) — not just that a crossover fired.

**Acceptance test after any bitstream rebuild:**
```bash
python3 host/bridge.py --port /dev/ttyUSB1 --source selftest --fast 8 --slow 32
```

**Stopping the system:** **Ctrl+C** in the terminal running `order_manager.py`.
It shuts down gracefully rather than just dying:
- the bridge stops sending/reading ticks
- the full session summary prints — echo/sent counts, per-strategy
  verified/diverged, the comparison table, fees, and the tax estimate
- the serial port, Alpaca WebSocket (if running), and the dashboard's HTTP
  server all close cleanly
- `--log`/`--audit` JSONL files get their final lines flushed and closed

Press it once and wait for the summary to print before the prompt returns —
a second Ctrl+C mid-shutdown can occasionally cut the summary off. After
stopping, `tail audit.jsonl` shows the shutdown event and final positions,
and `ls om.kill` should find nothing unless the kill switch tripped. If a
dashboard tab is still open in the browser, it'll just show fetch errors
in the console — harmless; close the tab or refresh once a new session is
running. If Ctrl+C doesn't return control within a few seconds (rare —
usually a serial port stuck in a bad state), Ctrl+C again, or as a last
resort Ctrl+Z then `kill %1` from that terminal — but that skips the
summary, so treat it as an escape hatch, not routine.

---

**A fourth comparison row, `--profit-gate`,** scores the SAME SMA
crossover signals with one added rule: a sell only executes if price
is strictly above the average cost of shares currently held — refusing
to realize a loss. Score-only, never trades, regardless of
`--strategy`. Worth knowing before reading its numbers: its win rate is
trivially 100% by construction (a loss can never be realized here, so
every closed trip is definitionally a win) — that's not a sign of
quality, it's just the rule. This is the textbook "disposition effect"
from behavioral finance (holding losers too long, selling winners
quickly); the published literature on it generally finds it *hurts*
returns, since a position that never recovers just sits open
indefinitely instead of realizing a small, bounded loss. Watch the
`open` indicator (`*`) and the block-reason breakdown (`would realize
a loss x...`) alongside net $, not just the win percentage.

## Historical Data & Backtesting

Live sessions produce a handful of round trips at best — every
comparison report so far has printed "few round trips, treat as
anecdote." Backtesting against real historical data is how you get a
statistically meaningful sample (a real 5-year SPY backtest produced 95
trips per strategy) instead of n=4.

### 1. Fetch historical trades

```bash
# one symbol
python3 host/fetch_historical_trades.py --symbol SPY --years 3

# several symbols, fetched CONCURRENTLY (independent page sequences,
# one shared rate limiter — this is the case where concurrency helps;
# a single symbol's pages are strictly sequential, no way around that)
python3 host/fetch_historical_trades.py --symbols SPY,QQQ,AAPL --years 3
```

Notes:
- **Resumable.** Ctrl+C any time; rerun the exact same command to
  continue from the checkpoint instead of starting over.
- **Files are scoped to the exact range requested** —
  `SPY_2026-01-01_2026-07-01.trades.jsonl` — so fetching a different
  range for the same symbol never collides with or silently shadows an
  earlier fetch. If you have old flat `SPY.trades.jsonl` files from
  before this, they're stale; delete and refetch.
- **`--end` defaults to 2 days before today**, not today — free-tier
  SIP access rejects queries that touch genuinely recent data (HTTP
  403, "subscription does not permit querying recent SIP data"). If
  you need the last day or two, use `--feed iex` for that slice
  specifically (no recency restriction on IEX), or just accept the
  small gap — irrelevant for a multi-year statistical backtest.
- Start small first: `--start 2026-06-01 --end 2026-07-01` to validate
  the whole pipeline before committing to a multi-hour, multi-year
  pull. Raw tick-level history for a liquid symbol is large.
- `--rate-per-min` defaults to 180 (of the free tier's 200/min cap);
  transient rate-limit hits and connection drops retry with backoff
  automatically.

### 2. Run the backtest

```bash
python3 host/backtest.py --trades historical_trades/SPY_2026-01-01_2026-07-01.trades.jsonl \
    --symbol SPY --strategy sma --fast 8 --slow 32 --cooldown 60
```

`--max-shares` (10), `--max-notional` ($2,000), and `--max-orders-per-day`
(1000) match what `order_manager.py`'s live CLI used before v3.27 — a bare
backtest with no risk-limit flags reproduces that earlier position-sizing
philosophy. `order_manager.py` itself moved to dollar-exposure sizing in
v3.27 (`--max-position-notional`, no more `--max-shares`); `backtest.py`
deliberately kept the share-count model, since offline analysis has no
reason to inherit a live-trading-specific sizing preference — see the full
CLI reference below for exactly what each tool's defaults are now.
what a default live session would have done, not a separately-tuned
set of backtest-only assumptions.

- Replays the file through the **exact same** `SMAMirror`/`EMAMirror`/
  `StrategyScorecard`/`RiskPolicy` classes the live system and hardware
  verification use — not a reimplementation, so a backtest result is
  guaranteed consistent with what the FPGA actually computes.
- `--strategy` only controls which row is labeled `[LIVE]` in the
  report (cosmetic) — **both rows are gated replays** in a backtest,
  neither has real fills. `RiskPolicy`'s cooldown and daily-order-cap
  gating evaluate against each trade's own *historical* timestamp, not
  wall-clock time while the script runs — replaying years of data in
  seconds still gets correct day-by-day and cooldown gating.
- **`--profit-gate`** adds a third row, `SMA profit-gated` — the same
  SMA crossover signals with one rule added: a sell only executes if
  price is above the average cost of shares held (see
  `compare.py`'s `ProfitGatedScorecard`; also available live via
  `order_manager.py --profit-gate`). Always score-only, regardless of
  `--strategy`. Its win rate is trivially 100% by construction (a loss
  can never be realized here) — that's not a quality signal, watch net
  $ and the `would realize a loss` count in the block-reason breakdown
  instead.
- **`--htf-ltf`** adds a multi-timeframe trend-alignment strategy (see
  `htf_ltf_strategy.py`): a higher-timeframe 20/50/200 EMA stack sets a
  long-only bullish/bearish/none bias; a lower-timeframe fast/slow EMA
  cross times entries, only in the bias direction; the position then
  trails until a lower-timeframe bar closes back below its fast EMA —
  independent of whether the higher-timeframe bias has technically
  reversed yet. `--htf-interval`/`--ltf-interval` set each timeframe's
  bar size in seconds (default 3600/300 = 1 hour / 5 minutes). Two
  scope notes worth knowing: it's **long-only** (a bearish HTF bias
  means stay flat, not short — nothing in this project shorts), and it
  uses the **exact textbook EMA formula** (alpha = 2/(N+1)), not the
  power-of-two alpha every other engine here uses for FPGA-friendly
  shift arithmetic — this strategy never runs in fabric, so there's no
  reason to force that approximation. This is also the first strategy
  that needs OHLC bars rather than raw ticks, since "the daily chart"
  and "the 15-minute chart" aren't concepts the tick-level engines have
  any notion of.
- **`--vwap-bounce`** (`--vwap-band-k` sets band width, default 1.0
  session standard deviations) — a session-VWAP mean-reversion
  strategy: buy when price dips below a volume-weighted-stdev band
  under VWAP and bounces back above it, sell when price reverts back
  up to VWAP itself. Positions are forced flat at each day's session
  boundary (VWAP only means anything within the session it's computed
  over) — this also happens to bound the worst case at one trading
  day, the same disposition-effect risk flagged for `--profit-gate`.
  Unlike HTF/LTF, this one genuinely reacts to every tick the way
  SMA/EMA do, and is a real fetch_historical_trades.py field this
  project hadn't used until now — trade volume (Alpaca's `"s"` field),
  already sitting in every downloaded historical file, unused until
  this strategy needed it.
- **`--pg-max-hold-days`** (default 5.0) bounds `--profit-gate`'s
  never-realize-a-loss rule: a position held longer than this many
  days force-closes at the next signal's price, even at a loss,
  through the same `RiskPolicy` gate as any other sell (see
  `compare.py`'s `ProfitGatedScorecard.max_hold_days`). Multi-year
  VTI/QQQ backtests made the case for this directly — "would realize
  a loss" was the single largest gated-away reason (1.4M+ signals on
  VTI, 4.2M+ on QQQ), and the row's perpetual `1 open` position at
  report end was carrying unbounded unrealized loss the net-$ column
  structurally couldn't show. With the bound in place, win rate is a
  real number again — a forced exit below cost counts as a loss —
  and `forced_exits` in the saved `summary.json` tells you how often
  it fired. Hold time is measured from the *first* lot bought since
  flat, so averaging down can't extend a loser's clock. Pass `--pg-
  max-hold-days 0` to restore the original unbounded behavior, for
  an A/B against the bounded version.
- **`--blended`** adds a two-sleeve portfolio row, `Blend
  (VWAP+SMA-PG)`, combining `--vwap-bounce` and `--profit-gate` as
  independently-capitalized sleeves rather than one filtering the
  other (see `blended_strategy.py`). The case for a *blend* over a
  *filter*: monthly net P&L between the two strategies correlates
  around **-0.15** on both VTI and QQQ multi-year backtests — close
  to uncorrelated, with a mild diversifying tilt — and the SMA-PG
  sleeve sits out roughly a third of months where VWAP bounce is
  still trading. A filter changes trade selection and would need its
  own backtest from scratch; a blend only changes capital allocation,
  so each sleeve's own already-validated numbers still mean what they
  meant.
  - Each sleeve is the **unchanged** `VWAPBounceScorecard` /
    `ProfitGatedScorecard`, with its own `RiskPolicy` clone built from
    its own carved-down `RiskLimits` — own cooldown, own daily cap,
    own `max_shares`/`max_notional`. One sleeve's cooldown never
    blocks the other's signal.
  - One `AccountExposureCap` sits above both sleeves: total open
    cost-basis notional across BOTH can never exceed one account
    ceiling, no matter what either sleeve's own per-order limits would
    allow alone. Stateless — recomputed from the sleeves' live
    positions on every check, so there's no separate ledger that can
    drift out of sync.
  - `--blend-vwap-shares`/`--blend-vwap-notional` (default 6 /
    $1,300) and `--blend-pg-shares`/`--blend-pg-notional` (default 4 /
    $700) size each sleeve; `--blend-account-notional` (default
    $2,000) sets the combined cap — by default this re-divides the
    same budget a single-strategy `--max-notional` would use, rather
    than adding capital.
  - The report row shows the merged totals, a sub-row per sleeve, and
    one summary line with the two numbers a per-strategy table can't
    show: **unrealized** P&L marked on whatever's still open (the
    exact figure the standalone profit-gated row's `net $` column
    always excluded), and the **combined realized max drawdown**
    across both sleeves in close order — a diversification argument
    built on monthly correlation can still hide same-week overlap
    that only a trade-by-trade drawdown catches.
  - `--blended` implies the SMA and VWAP machinery it feeds from; add
    `--profit-gate --vwap-bounce` alongside it to also print the
    standalone rows for a side-by-side comparison against the blend.
  - **Score-only, backtest/comparison only** — not yet wired into
    `order_manager.py` for live sessions. The scored-state restore
    that survives a live restart replays `"scored_signal"` audit
    events, but the VWAP sleeve consumes raw ticks (like the ladder
    strategy), which aren't in that audit log — a mid-day restart
    would silently reset its session VWAP. That gap needs closing
    before this row trades real (paper) fills.

  ```bash
  python3 host/backtest.py \
      --trades historical_trades/VTI_2023-07-17_2026-07-14.trades.jsonl \
      --symbol VTI --strategy sma --profit-gate --vwap-bounce --blended \
      --monthly
  ```
- **`--monthly`** prints (and saves) a month-by-month P&L breakdown for
  every row, bucketed by each trip's CLOSE date — from the SAME
  continuous run, not independent monthly backtests. That distinction
  matters: a position opened in one month and closed in the next is
  correctly attributed entirely to its close month, with its real
  entry price intact; splitting into independent monthly runs instead
  would either lose that position entirely or restart HTF/LTF's ~200-
  hour warmup every single month. Every month's numbers reconcile
  exactly to the overall total, by construction — same trips, just
  bucketed differently for display, never a second, independently-
  computed number that could disagree.
- **Combine incrementally-fetched ranges** without re-downloading, by
  passing multiple files in chronological order:
  ```bash
  --trades SPY_2026-01-01_2026-06-01.trades.jsonl,SPY_2026-06-01_2026-07-01.trades.jsonl
  ```
  (raises if the files are out of order or overlapping, rather than
  silently corrupting the replay's notion of historical time.)
- Read the block-reason breakdown (`gated-away signals: daily order cap
  x..., cooldown x...`) before drawing conclusions from the dollar
  figures — a strict cap dominating the blocks means you're looking at
  "the indicator under these risk limits," not the raw indicator's
  unconstrained behavior.

### 3. Every run is saved automatically — go back and review it later

No extra flag needed — `backtest.py` saves each run to
`./backtest_results/<symbol>_<start>_<end>_<timestamp>/` by default,
containing:

- **`summary.json`** — symbol, real date range (derived from the actual
  trade data replayed, not guessed from a filename), every strategy
  parameter used, and the full per-strategy results (signals, trips,
  wins, win rate, gross/fees/net, block reasons, open positions)
- **`report.txt`** — the exact human-readable comparison table, with
  the parameters listed above it

Rerunning the same symbol and date range with different parameters
never overwrites an earlier result — each run gets its own folder,
collision-proof even for two runs in the same second. Pass `--no-save`
to skip saving a quick throwaway run, or `--results-dir` to save
somewhere other than the default.

**Ctrl+C during a long backtest** (a multi-year, 100M+ trade replay can
take a while) still produces a report instead of crashing past it —
you get the partial comparison, printed and saved, for whatever
progress was made before you interrupted it. It's unmistakably marked
as partial in three places: an `-INTERRUPTED` folder-name suffix
(visible in `list_backtest_results.py`'s table without opening
anything), `summary.json`'s own `interrupted` field, and a banner at
the top of `report.txt` — never confusable with a complete run.

Browse what you've saved:
```bash
python3 host/list_backtest_results.py                  # every run, newest first
python3 host/list_backtest_results.py --symbol RKLB     # just one symbol
python3 host/list_backtest_results.py --folder RKLB_2023-07-16_2026-07-13_20260715-161454
                                                        # full report for one run
```

---



## New in this drop
| File | Purpose |
|---|---|
| `rtl/top_arty.sv` | Arty A7-100T top level — all 6 checklist items |
| `rtl/timestamp_us.sv` | Free-running 64-bit µs counter, latched on SOF, committed on good EOF |
| `constraints/top_arty.xdc` | Pins (clk E3, UART A9, reset C2, LEDs) + clock + false paths |
| `sim/tb_top_integration.sv` | Bit-level UART testbench of the whole chain — 31 PASS / 0 FAIL |
| `rtl/uart_rx.sv`, `rtl/tick_parser.sv` | Rebuilt to MANUAL.md §3 spec (originals were on the other machine). `tick_parser` gains one new output: `sof_seen` (1-cycle pulse), used by the timestamp latch. |

## Pin-name correction to MANUAL.md §4
The host→FPGA UART line on the Arty is `uart_txd_in` on **pin A9** (named from
the host FTDI's perspective). `uart_rxd_out` is pin **D10** and is the
FPGA→host TX line, reserved for the result-frame layer. The manual had the
right pin, wrong signal name.

## Simulate (iverilog)
```bash
iverilog -g2012 -o sim.out sim/tb_top_integration.sv \
    rtl/top_arty.sv rtl/uart_rx.sv rtl/tick_parser.sv rtl/timestamp_us.sv
vvp sim.out            # expect: RESULT: 31 PASS / 0 FAIL
gtkwave tb_top_integration.vcd   # optional
```
(The "unique qualities are ignored" notes from vvp are harmless — `unique case`
is a synthesis-quality hint for Vivado.)

## Build (Vivado)
1. New project → Arty A7-100T (xc7a100tcsg324-1).
2. Add sources: everything in `rtl/`. Set `top_arty` as top.
3. Add constraints: `constraints/top_arty.xdc`.
4. Add simulation source: `sim/tb_top_integration.sv`, sim top = `tb_top_integration`.
5. Synthesize → Implement → Generate Bitstream → Program.

## Bring-up smoke test (no host bridge needed yet)
LED3 should heartbeat (~0.75 Hz) immediately after programming. Then from any PC:
```python
import serial, struct, time
p = serial.Serial('/dev/ttyUSB1', 115200)   # Arty enumerates two ports; UART is usually the second
f = b'\xAA\x01' + b'AAPL' + struct.pack('>IHB Q'.replace(' ',''), 1823400, 100, 1,
                                        int(time.time()*1e6)) + b'\x55'
p.write(f)      # LED1 blinks ~0.3 s  -> frame decoded
p.write(f[:-1] + b'\xFF')   # LED0 blinks -> parse_error, counter increments
```

## Timestamp semantics
`arrival_us` counts µs **since FPGA reset**, not Unix time. The host bridge
should do a one-time offset calibration (or work with frame-to-frame deltas)
before interpreting `arrival_us − host_tstamp` as transit latency.

---

# TX / Echo Layer (second drop)

## New files
| File | Purpose |
|---|---|
| `rtl/uart_tx.sv` | UART transmitter, mirror of uart_rx; valid/ready handshake |
| `rtl/sync_fifo.sv` | Parameterized FWFT FIFO (default depth 16) decoupling decode rate from echo rate |
| `rtl/frame_tx.sv` | Pops tick records, serializes 30-byte FPGA→host echo frames (wire format in file header) |
| `sim/tb_tx_integration.sv` | Bit-level echo-path test with a UART monitor in the bench — 20 PASS / 0 FAIL |

`top_arty.sv` gained the TX chain, the `uart_rxd_out` (D10) output, a
`TX_FIFO_DEPTH` parameter, and a `tx_drop_count` debug register.
`timestamp_us.sv` gained a `pending_us` output — fixing a real one-cycle
race the TX testbench caught (echo carried the *previous* frame's arrival
timestamp; see module header comment).

## FPGA → host echo frame (30 bytes, big-endian)
```
0     SOF      0xBB
1     TYPE     0x81 trade echo / 0x82 quote echo (0x80 | original type)
2-5   SYMBOL   as received
6-9   PRICE    as received
10-11 QTY      as received
12    SIDE     as received
13-20 HOST_TS  echoed host timestamp (us)
21-28 FPGA_TS  FPGA arrival timestamp (us since reset)
29    EOF      0xCC
```

## Throughput note
Echo frames (30 B, ~2.60 ms) are larger than input frames (22 B, ~1.91 ms),
so *sustained* back-to-back input outruns the echo path. The FIFO absorbs
bursts; on overflow the newest tick is dropped from the echo stream only
(never from the decode path) and counted in `tx_drop_count`. Invariant:
`echoes + drops == ticks decoded`. Real tick rates sit far below this limit.

## Host-side loopback test
```python
import serial, struct, time
p = serial.Serial('/dev/ttyUSB1', 115200, timeout=2)
f = b'\xAA\x01' + b'AAPL' + struct.pack('>IHBQ', 1823400, 100, 1,
                                        int(time.time()*1e6)) + b'\x55'
p.write(f)
echo = p.read(30)
assert echo[0] == 0xBB and echo[-1] == 0xCC and len(echo) == 30
_, etype, sym, price, qty, side, host_ts, fpga_ts, _ = \
    struct.unpack('>BB4sIHBQQB', echo)
print(sym, price/10000, qty, 'fpga_ts(us since reset) =', fpga_ts)
```
Round-trip works when the printed fields match what you sent. `fpga_ts`
counts from FPGA reset — calibrate an offset once, or use deltas.

## Simulate both benches
```bash
iverilog -g2012 -o sim.out   sim/tb_top_integration.sv rtl/*.sv && vvp sim.out    # 31 PASS
iverilog -g2012 -o simtx.out sim/tb_tx_integration.sv  rtl/*.sv && vvp simtx.out  # 20 PASS
```

---

# Indicator Engine Layer (third drop)

## New files
| File | Purpose |
|---|---|
| `rtl/indicator_engine.sv` | SMA crossover detector: running-sum windows, power-of-two divide-by-shift, warm-up gating, 3-stage pipeline |
| `sim/tb_indicator.sv` | Unit bench with a full mirror model checked on EVERY tick, incl. a 200-tick pseudo-random walk — 850 PASS / 0 FAIL |
| `sim/tb_indicator_e2e.sv` | End-to-end: UART frames in -> one 0x83 signal frame out, SMAs verified against hand-computed values — 14 PASS / 0 FAIL |

`top_arty.sv` gained parameters `TARGET_SYMBOL` (default "SPY "), `FAST_N` (8),
`SLOW_N` (32), `SIG_FIFO_DEPTH` (4); the engine is instantiated on the tick_*
bus; signals go through a dedicated HI-priority FIFO. `frame_tx.sv` now
arbitrates two FIFOs — signals always beat echoes, per-frame, never mid-frame.

## Signal frame (TYPE 0x83) — tagged union of the 30-byte format
```
1     TYPE     0x83
2-5   SYMBOL   target symbol
6-9   PRICE    trade price that triggered the crossover
10-11 QTY      0x0000
12    SIDE     0x01 golden cross (BUY) / 0x02 death cross (SELL)
13-16 SMA_FAST FPGA-computed fast SMA  (replaces HOST_TS bytes for this type)
17-20 SMA_SLOW FPGA-computed slow SMA
21-28 FPGA_TS  arrival timestamp of the triggering trade
```
Crossover convention: "above" = (fast > slow), strictly; first evaluation
after warm-up primes state and never fires. Host models must match both
rules (and the truncating integer shift-divide) to agree with the hardware.

## Regression status (all four benches)
```
tb_indicator        850 PASS   engine vs mirror model, incl. 27-crossover random walk
tb_indicator_e2e     14 PASS   full path, signal priority, no drops
tb_top_integration   31 PASS   RX layer regression
tb_tx_integration    20 PASS   TX/echo layer regression
```

## Hardware defaults
FAST_N=8 / SLOW_N=32 on "SPY " trades: the engine warms up after 32 SPY
trades, then emits an 0x83 frame on every fast/slow crossover. Rebuild the
bitstream (add indicator_engine.sv) — pins and XDC are unchanged.

---

# Host Bridge Layer (fourth drop) — `host/`

| File | Purpose |
|---|---|
| `host/tick_protocol.py` | Both frame codecs, resyncing `FrameParser`, and `SMAMirror` — the third implementation of the SMA spec, with exact hardware integer semantics (truncating shift, strict `>`, warm-up + priming) |
| `host/bridge.py` | The bridge: tick sources -> 22-byte frames out, echo/signal frames in, echo-driven model updates, order-agnostic `SignalVerifier`, latency stats, JSONL logging |
| `host/fpga_emulator.py` | A virtual Arty on a pty — develop and test the bridge with no board attached |
| `host/test_host.py` | 37 checks: silicon-anchored golden vectors, codec roundtrips, resync torture, and a full closed-loop Bridge<->Emulator run |

## Quick start (no hardware, terminal A / terminal B)
```bash
cd host
python3 fpga_emulator.py --symbol "SPY " --fast 8 --slow 32   # prints /dev/pts/N
python3 bridge.py --port /dev/pts/N --source sim --n 200 --rate 20
```

## Against the real board
```bash
# acceptance test of a fresh bitstream (params must match the build):
python3 bridge.py --port /dev/ttyUSB1 --source selftest --fast 8 --slow 32
# synthetic load with tick logging:
python3 bridge.py --port /dev/ttyUSB1 --source sim --n 500 --rate 50 --log ticks.jsonl
# live market data (Alpaca IEX feed, paper account keys):
pip3 install websocket-client --break-system-packages
ALPACA_KEY=... ALPACA_SECRET=... python3 bridge.py --port /dev/ttyUSB1 --source alpaca --symbol SPY
```

## Verification model (see bridge.py docstring for full reasoning)
The local `SMAMirror` ingests a trade only when its ECHO returns — only
ticks the FPGA provably decoded. Signals are matched by CONTENT within a
grace window, because wire order is legitimately ambiguous (the signal
FIFO can overtake queued echoes, or trail them when TX is idle). Every
0x83 frame's SMA_FAST/SMA_SLOW is compared bit-for-bit against the model;
any mismatch prints a DIVERGENCE and fails the session — the input a kill
switch will consume in the order-manager layer.

## Remaining roadmap
| Layer | Status |
|---|---|
| Host order manager (risk limits, kill switch, Alpaca paper REST) | next |
| Phone notifications (ntfy.sh on signals) | optional |

---

# Order Manager Layer (fifth drop) — project complete end-to-end

| File | Purpose |
|---|---|
| `host/order_manager.py` | Verified signals -> RiskPolicy -> broker. Latching kill switch (marker file, human re-arm), broker-of-record position reconciliation, JSONL audit of every decision including refusals. MockBroker + stdlib-only AlpacaPaperBroker (structurally refuses non-paper URLs). |
| `host/test_order_manager.py` | 25 checks — mostly of what gets REFUSED: pyramiding, position/notional caps, cooldown, daily cap, sell-when-flat, kill latching + re-arm, rejection escalation, plus the full emulator->bridge->OM->mock chain |

## Strategy (deliberately minimal)
Long-only, one symbol. Verified BUY -> buy `--qty` if flat; verified SELL ->
close position. Any model/hardware divergence trips the kill switch;
3 consecutive broker rejections trip it too. A tripped kill writes `om.kill`
and every future session refuses to start until a human deletes it.

## Run the whole system
```bash
# dress rehearsal, no hardware, no network (two terminals):
python3 host/fpga_emulator.py --symbol "SPY " --fast 8 --slow 32
python3 host/order_manager.py --port /dev/pts/N --source sim --broker mock \
        --cooldown 0 --n 300 --rate 20

# real board, mock broker:
python3 host/order_manager.py --port /dev/ttyUSB1 --source sim --broker mock --cooldown 0

# the full loop — live IEX ticks, FPGA decisions, Alpaca PAPER orders:
ALPACA_KEY=... ALPACA_SECRET=... \
python3 host/order_manager.py --port /dev/ttyUSB1 --source alpaca \
        --broker alpaca --symbol SPY --qty 1 --max-position-notional 500
```
(For real-money live trading and replaying real historical data against
the board, see "Live Trading Enablement" and "VWAP FPGA Engine & Live
Strategy Selection" further down — both grew significant new interlocks
and capability well past this basic paper-trading example.)

## Full-system status
```
RTL benches:  31 + 20 + 850 + 14 = 915 checks   (RX, TX, engine unit, engine e2e)
Host suites:  37 + 25            =  62 checks   (protocol/bridge, order manager)
```
Pipeline: Alpaca WebSocket -> host bridge -> UART -> tick_parser -> SMA
crossover engine -> priority TX -> verified 0x83 -> risk policy -> Alpaca
paper order. Optional remaining: ntfy.sh push notifications on fills.

## `order_manager.py` — full command-line reference

Grown a lot since this section was first written (see the later drops
below for VWAP, historical replay, live trading, and cost estimation).
Every flag, grouped by what it actually controls — pulled from
`--help` and the code, not reconstructed from memory, so treat this as
current for the wire-format/parameter versions elsewhere in this file.

**Connection**
| Flag | Default | What it does |
|---|---|---|
| `--port` | *required* | Serial device (e.g. `/dev/ttyUSB1`, or a pty from `fpga_emulator.py`) |
| `--symbol`, `--symbols` | `SPY` | Comma-separated, up to 8 (e.g. `SPY,QQQ,AAPL`) |
| `--baud` | `921600` | Must match the bitstream's `BAUD` parameter — 115200 for anything built before that change |

**Must match the bitstream's build parameters** (mismatches show up as
false `DIVERGENCE` lines, not wrong values — the host mirror and the
fabric are just computing against different assumptions)
| Flag | Default | What it does |
|---|---|---|
| `--fast` | `8` | SMA fast window, in ticks |
| `--slow` | `32` | SMA slow window, in ticks |
| `--ema-kf` | `3` | Fast EMA shift (alpha = 2⁻ᵏ) |
| `--ema-ks` | `5` | Slow EMA shift |
| `--vwap-warmup` | `20` | Ticks before the fabric VWAP engine allows events (`VWAP_WARMUP`) |
| `--vwap-k2-q8` | `256` | VWAP band width, k² in Q8 fixed point (`VWAP_K2_Q8`; 256 = k of 1.0) — NOT the same as `--vwap-band-k` below, which tunes a different, host-only VWAP calculation |

**Which strategy trades**
| Flag | Default | What it does |
|---|---|---|
| `--strategy` | `sma` | `sma`, `ema`, or `vwap_bounce` — this one's verified signals actually place orders; the other two are scored alongside for comparison, gated identically, never trading |

**Extra scored-only comparisons** (never trade, regardless of `--strategy`)
| Flag | Default | What it does |
|---|---|---|
| `--ladder` | off | Adds a weekly-anchored buy-the-dip ladder row (`ladder_strategy.py`) |
| `--ladder-step` | `0.03` | Trigger spacing between ladder levels (3%) |
| `--ladder-levels` | `3` | Max buy levels before the ladder is "full" |
| `--ladder-qty` | `1` | Shares bought at each level |
| `--ladder-method` | `week_vwap` | How each symbol's weekly baseline is computed (`friday_close`, `week_avg_close`, `week_vwap`, `week_midpoint`) |
| `--ladder-baseline` | none | Manual override, e.g. `SPY:500.00,QQQ:450.00` — skips the Alpaca weekly-bars fetch; required for `--source sim` |
| `--vwap-bounce` | off | Adds a HOST-computed session-VWAP mean-reversion row, one per symbol — a different implementation from the fabric engine, useful for comparing the two |
| `--vwap-band-k` | `1.0` | That host-only row's band width, directly in session standard deviations |
| `--profit-gate` | off | Adds an SMA-crossover row with one rule added: never sell below cost basis |
| `--pg-max-hold-days` | `5.0` | `--profit-gate` only: force-close a position held longer than this many days, even at a loss — bounds the never-sell-at-a-loss rule's unbounded downside. `<= 0` disables (restores unbounded) |

**Data source**
| Flag | Default | What it does |
|---|---|---|
| `--source` | `sim` | `sim` (synthetic random walk), `alpaca` (live/paper market data), or `historical` (real recorded trades) |
| `--n` | `200` | `--source sim` only: number of synthetic ticks |
| `--rate` | `10.0` | `--source sim` only: ticks/sec |
| `--start-price` | `500.0` | `--source sim` only: starting price for the random walk |
| `--trades` | none | `--source historical` only: one or more JSONL files from `fetch_historical_trades.py`, comma-separated, chronological order — required, single symbol only |
| `--replay-rate` | `200.0` | `--source historical` only: ticks/sec cap (does not reproduce real recorded gaps between ticks) |
| `--replay-max` | `20000` | `--source historical` only: stop after this many trades; `0` or negative means no cap |

**Broker & risk limits** (apply to whichever strategy is live)
| Flag | Default | What it does |
|---|---|---|
| `--broker` | `mock` | `mock` (simulated fills) or `alpaca` (paper or live, depending on `--live`) |
| `--live` | off | REAL MONEY. Requires `--broker alpaca` plus the full interlock chain — see "Live Trading Enablement" below |
| `--max-daily-loss` | none | $ realized loss that halts the session; MANDATORY when `--live` |
| `--qty` | `5` | Shares bought per entry |
| `--max-position-notional` | `10000.0` | Max dollar value ($) of the TOTAL position (existing holdings + this buy, at current price) — v3.27: replaces the old share-count `--max-shares` cap with a dollar-exposure cap. `RiskLimits.max_shares` still exists underneath for `backtest.py` and the blend strategy's own per-sleeve allocation, but this CLI no longer exposes or is constrained by it |
| `--max-notional` | `3000.0` | Max dollar value ($) of any SINGLE buy order (`qty × price`) — independent of `--max-position-notional` above, which caps the total position, not one order |
| `--max-orders-per-day` | `1000` | Daily order cap |
| `--cooldown` | `60.0` | Minimum seconds between orders |
| `--ignore-market-hours` | off | For mock/off-hours testing; has NO EFFECT when `--live` is set — `require_market_hours` is forced on unconditionally in live mode, silently overriding this flag rather than refusing it as an error |

**Verification tuning**
| Flag | Default | What it does |
|---|---|---|
| `--verify-grace-s` | `2.0` | Real seconds an unmatched FPGA/model signal may wait before the kill switch trips on "orphan signal" — raise this if that divergence recurs during genuinely high signal-volume periods rather than a real hardware fault |

**Logging & audit**
| Flag | Default | What it does |
|---|---|---|
| `--log` | none | Bridge tick JSONL path |
| `--audit` | `om_audit.jsonl` | Order/decision audit log — also what restart-restore reads to rebuild today's fills and scored-signal history |

**Cost & tax estimate** (works in any mode; figures are real fees/tax for
`--live` fills, purely hypothetical estimates for paper fills — see
"Costs & Tax Estimation" below)
| Flag | Default | What it does |
|---|---|---|
| `--household-income` | none | Taxable household income for the after-tax estimate |
| `--gross` | off | Treat `--household-income` as gross; subtracts the 2026 standard deduction |
| `--filing-status` | `mfj` | `single` or `mfj` |
| `--state-rate` | `4.40` | Flat state income tax %, applied on top of federal (default: Colorado) |

**Monitoring & diagnostics**
| Flag | Default | What it does |
|---|---|---|
| `--dashboard` | none | Serve the web console on this port (e.g. `8000`) |
| `--selftest` | off | Hardware acceptance test: deterministic stimulus, verified against host models, then exits — no broker, no trading, no dashboard. Run this first after any bitstream change |

---

## Running without an FPGA board (v3.29)

Everything above assumes a real board on `--port`. As of v3.29,
`fpga_emulator.py` — previously an internal test fixture only — is a
genuine, documented replacement for one, so this project can run as a
pure-Python terminal tool with no board attached at all (freeing the
Arty up for something else, or just for development away from the
hardware).

**This is not an approximation.** The emulator computes every signal
through the exact same `SMAMirror`/`EMAMirror`/`VWAPMirror` classes
that every hardware signal in this project has always been verified
against — the same math the real board's RTL is checked against
bit-for-bit. Running against the emulator doesn't lower the bar; it
removes the silicon underneath math that was already proven correct.

```bash
# terminal 1: start the virtual board, leave it running
python3 host/fpga_emulator.py --symbol SPY
```

`--symbol` here is only a startup placeholder for slot 0 — it gets fully
overwritten the moment `order_manager.py` connects and configures its
own real symbol list (`configure_symbols()` rewrites all 8 slots
unconditionally). It doesn't matter what you put there; leaving it at
the default is fine.

```bash
# terminal 2: point order_manager.py at the printed stable path exactly
# like a real board -- everything else about the command is unchanged
python3 host/order_manager.py --port /tmp/fpga-tick-emulator \
    --baud 115200 --source alpaca --broker alpaca \
    --symbols SPY,QQQ,RKLB,TSLA,RIVN --strategy vwap_bounce \
    --dashboard 8000 --household-income 185000 \
    --log ticks.jsonl --audit audit.jsonl
```

**On macOS, add `--baud 115200` explicitly** (as shown above) —
`order_manager.py`'s default (`921600`) needs a special ioctl macOS
only allows on real serial hardware, not a pty. `bridge.py` has a v3.30
fallback that catches this automatically if you forget, but setting it
explicitly avoids the one-time warning message and is one less thing to
debug if something else goes wrong. `--source alpaca` must be stated
explicitly — `--source` defaults to `sim` (a synthetic random walk),
not `alpaca`, so omitting it silently runs a fake session instead of
connecting to real market data. `--broker alpaca` without `--live` is
paper trading against your real Alpaca account, using the
`ALPACA_KEY`/`ALPACA_SECRET` environment variables, same as any other
machine.

A healthy session prints `verified: FPGA vwap N == model — hardware
math confirmed` for each signal and ends with `RESULT: OK` — if you
ever see `DIVERGENCE` instead, stop and investigate before trusting
anything further from that session.

The emulator prints a **stable symlink path** (`/tmp/fpga-tick-emulator`
by default, override with `--port-symlink`) alongside the real,
otherwise-different-every-run pty path — so your `order_manager.py`
command line never has to change between restarts, the same way a
real board's `/dev/ttyUSB0` stays put across sessions (modulo the USB
enumeration quirks a real board can have, which the emulator has none
of). The symlink is cleaned up automatically on shutdown, whether you
stop it with Ctrl-C or a plain `kill <pid>`.

**Keep terminal 1 running for the entire session.** If it's closed,
killed, or crashes, terminal 2 will still *open* its connection
successfully (the pty path still technically exists) but nothing will
be listening on the other end — every tick will silently go nowhere,
`ticks sent`/`echoes received` will both read 0, and the session ends
almost immediately with no crash and no error, just an empty summary.
If that happens, check terminal 1 is still alive and showing its normal
output before assuming anything else is wrong.

Match parameters the same way you would with a real bitstream:
`--fast`/`--slow`/`--ema-kf`/`--ema-ks`/`--vwap-warmup`/`--vwap-k2-q8`
all exist on the emulator too, defaulting to the same values
`order_manager.py` defaults to — so a bare `fpga_emulator.py` and a
bare `order_manager.py` already agree with each other out of the box.

Everything else is completely unaffected: `--selftest`, `--source
historical`, `--strategy vwap_bounce`, the dashboard, the live-arming
interlocks — all of it works identically, because none of it has ever
cared whether the bytes on `--port` came from real silicon or from
`fpga_emulator.py`. The only thing that changes is what's plugged into
the USB port: nothing.

---

# Costs & Tax Estimation (sixth drop)

| File | Purpose |
|---|---|
| `host/costs.py` | Sell-side regulatory fee schedule (SEC §31 $20.60/M eff. 2026-04-04; FINRA TAF $0.000195/sh, $9.79 cap, eff. 2026-01-01), 2026 federal bracket engine, marginal short-term-gains tax estimator (+3.8% NIIT, flat state rate), `CostTracker` for realized P&L |
| `host/test_costs.py` | 30 checks incl. published anchors (single $200k -> ~$40,600; MFJ -> ~$33,400), TAF cap, bracket spanning, NIIT threshold, losses |

Every fill now records fees and realized P&L in the audit log, and the
session summary adds an after-tax view when household income is given:

```bash
python3 host/order_manager.py --port /dev/ttyUSB1 --source sim --broker mock \
    --cooldown 0 --household-income 185000 --filing-status mfj --state-rate 4.4
# add --gross if the income figure is pre-deduction; add --single as needed
```

Notes: short-term gains are taxed as ORDINARY income stacked on household
income (nothing here is ever held a year); fees apply to sells only; rates
are constants at the top of costs.py — verify annually against sec.gov /
finra.org / IRS Rev. Proc. ESTIMATES ONLY, not tax advice; paper trades
incur no real fees or tax.

---

# Live Trading Enablement (seventh drop) — OFF by default, six interlocks

**Warning first:** a bare SMA tick-crossover is a demonstration strategy.
After spread and slippage its live expected value is negative. Do not arm
live until the SAME configuration has run profitably on paper for weeks,
and then only at minimum size. The interlocks below make arming deliberate;
they do not make the strategy good.

## The interlock chain (ALL must pass, tested individually)
1. `--live` flag (and `--broker alpaca`)
2. `ALPACA_LIVE_KEY` / `ALPACA_LIVE_SECRET` — deliberately DISTINCT env vars
   from the paper keys, so paper credentials can never silently arm live
3. `ALPACA_LIVE_ACK=I-UNDERSTAND-THIS-TRADES-REAL-MONEY`
4. `--max-daily-loss` — MANDATORY in live; realized net loss breaching it
   trips the (latching) kill switch mid-session
5. Interactive terminal required — no scripted/cron live starts
6. Operator retypes `LIVE <SYMBOL>` after a banner restating every limit
   (two-key discipline: confirmation restates parameters)

Market hours enforcement cannot be disabled in live. The AlpacaLiveBroker
class independently re-checks the ack phrase (defense in depth), and
`test_live_gating.py` (21 checks) knocks out each gate one at a time and
asserts refusal.

## Arming (after weeks of profitable paper — not before)
```bash
export ALPACA_LIVE_KEY=... ALPACA_LIVE_SECRET=...
export ALPACA_LIVE_ACK=I-UNDERSTAND-THIS-TRADES-REAL-MONEY
python3 host/order_manager.py --port /dev/ttyUSB1 --source alpaca \
    --broker alpaca --live --symbol SPY --strategy vwap_bounce --qty 1 \
    --max-position-notional 1000 --max-daily-loss 50 \
    --household-income 185000
# banner shows: symbol SPY / strategy VWAP_BOUNCE <-- this one TRADES
# ...then type: LIVE SPY VWAP_BOUNCE
```
`--strategy` also accepts `sma` or `ema` — v3.24: the confirmation phrase
now names the strategy, not just the symbol (`arm_live_trading()` takes it
as a required argument). With three tradeable strategies possible, a typo'd
`--strategy` or a stale saved command line could otherwise arm live trading
against a different strategy than intended with nothing in the banner or
confirmation to catch it. `--qty`/`--max-position-notional`/`--max-notional`
default to 5 shares/$10,000/$3,000 (v3.27) — shown overridden tighter here
(`--qty 1 --max-position-notional 1000`) since this is a first real-money
arming example, not a recommendation to start at the defaults.

## Final test census
```
RTL:   31 + 20 + 850 + 14        =  915
Host:  37 + 25 + 30 + 21         =  113
Total                            = 1028 checks, 0 failures
```

---

# Web Console (eighth drop)

| File | Purpose |
|---|---|
| `host/dashboard.py` | Zero-dependency web console served by the order-manager process: scope chart (price + SMAs + BUY/SELL markers), on-screen LED strip mirroring the Arty's physical LED semantics, position/P&L/fees/RTT/verification stats, signal & event logs, and a guarded two-click KILL SWITCH wired to the latching halt |
| `host/test_dashboard.py` | 23 checks: page self-containment, /api/state mirrors the live objects, POST /api/kill trips the latch |

```bash
python3 host/order_manager.py --port /dev/ttyUSB1 --source sim \
    --broker mock --cooldown 0 --dashboard 8000
# then open http://localhost:8000  (or http://<machine-ip>:8000 from a phone)
```

Design notes: fully self-contained page (system fonts, no CDN) so a bench
machine without internet still gets the console; the dashboard can HALT the
system but can take no other money-touching action — killing is the
fail-safe direction. The kill is two-click guarded client-side and latching
server-side (same om.kill marker discipline).

## Final test census
```
RTL:   31 + 20 + 850 + 14           =  915
Host:  37 + 25 + 30 + 21 + 23      =  136
Total                              = 1051 checks, 0 failures
```

---

# Second Strategy: EMA Crossover (ninth drop) — compare two algorithms

| File | Purpose |
|---|---|
| `rtl/ema_engine.sv` | EMA crossover in fabric: extended-precision leaky-integrator accumulators (A = ema<<K, A' = A + p - (A>>K)) — no window RAM, no multiply, no truncation deadband. Same warm-up/priming/pipeline discipline as the SMA engine. Wire type 0x84. |
| `sim/tb_ema.sv` | 1900 checks vs a mirror model, incl. exact-convergence (deadband) proof and a 300-tick walk (37 crossings, all matched) |
| `host/compare.py` | StrategyScorecard: both strategies scored hypothetically (long-only, 1 share, signal-price fills, sell fees) from the same verified stream; honest about slippage-free fills and n-too-small |

Both engines run IN PARALLEL on the FPGA off the same tick bus, with a
same-cycle collision arbiter into the priority signal FIFO (both pipelines
are 3 stages, so simultaneous crossings happen — the e2e bench forces one
deliberately). Host: EMAMirror (silicon-anchored goldens 1399/1725 ->
2200/1884), per-strategy verifiers, dual SMA/EMA traces on the dashboard
(dashed = EMA) plus a live comparison panel.

Only ONE strategy trades (`--strategy sma|ema`, default sma); the other is
scored, not traded:
```bash
python3 host/order_manager.py --port /dev/ttyUSB1 --source sim --broker mock \
    --cooldown 0 --strategy sma --dashboard 8000
# bitstream params must match: --fast/--slow/--ema-kf/--ema-ks
```

Sample comparison from a 400-tick random walk (both lose — random walks
have no signal to extract; whipsaw + strict crossover = death by a
thousand $5 cuts. The point is the harness, and it also demonstrates
exactly why the live-mode warnings exist):
```
  strategy       signals  trips  win     gross $      net $
  SMA 4/8             52     25   28%     -129.37    -129.93
  EMA 1/2:1/8         30     14   14%      -89.10     -89.39
```

## Final test census
```
RTL:   31 + 20 + 850 + 1900 + 23        = 2824
Host:  65 + 25 + 25 + 30 + 21           =  166
Total                                   = 2990 checks, 0 failures
```

---

# v2.0 — Runtime Symbol Configuration (wire format v2)

The traded symbol set is now RUNTIME state, not a synthesis constant: eight
slots in a fabric register file (`rtl/symcfg.sv`), written over the same
UART link, edited live from the dashboard.

**Wire format v2** (breaking): SYMBOL grows 4 -> 6 bytes to fit every
S&P 500 ticker (GOOGL, BRK.B). Tick frames 22 -> 24 B (SYMBOL at 2-7),
FPGA frames 30 -> 32 B. New TYPE 0x10 = slot write (QTY[2:0] = slot,
SIDE = set/clear); its echo (0x90) is the write ACK — the host reads back
what the fabric actually latched, through the same path as data.

**Semantics that matter:**
- Writing a slot ALWAYS resets that slot's engines (`slot_wr` ->
  `state_rst`): a reconfigured symbol starts with empty windows on both
  sides, so host mirror models rebuild in lockstep. Proven in
  tb_indicator_e2e (rewrite slot 0, verify the old warm state is gone).
- One tick = one symbol, so per strategy at most one of the 8 lanes fires
  per tick; a lowest-slot priority encoder folds lanes into one record.
  The SMA-vs-EMA same-cycle pend arbiter is unchanged.
- `Bridge.configure_symbols()` writes all 8 slots, blocks for all 8 ACKs,
  then rebuilds models/verifiers/counters — hardware and host change
  atomically or the call fails loudly.

**Host:** per-(strategy, symbol) mirror models and verifiers; per-symbol
positions (long-only each), cost entries, and scorecard opens; multi-walk
sim source; Alpaca subscribes the whole list and resubscribes on live
reconfiguration. `--symbols SPY,QQQ,AAPL` everywhere.

**GUI:** an 8-slot editor writes the FPGA over UART and reports the acked
ground truth; chart gains a symbol selector; signal tables gain SYM.

**Bitstream rebuild is REQUIRED** (new symcfg.sv + parser/frame width
changes). A v1 bitstream against v2 host software fails selftest with
framing/ack DIAG lines — that mismatch is detected, not silent.

## Final test census (v2.0)
```
RTL:   31 + 20 + 850 + 1900 + 36        = 2837
Host:  83 + 27 + 31 + 31 + 21           =  193
Total                                   = 3030 checks, 0 failures
```

---

# VWAP FPGA Engine & Live Strategy Selection (v3.17 – v3.24)

A third strategy, built in fabric from scratch after its host-only version
(`vwap_bounce_strategy.py`) earned it on multi-year backtests: session-VWAP
mean-reversion, computed in hardware instead of on the host, verified the
same way SMA/EMA always have been, and now selectable as the strategy that
actually trades.

**RTL** (`rtl/vwap_engine.sv`, `rtl/sessctl.sv`): a shared serial divider
(no DSP-hungry single-cycle divide needed — ticks arrive orders of
magnitude slower than the ~200-cycle evaluation budget), a band test done
entirely in the squared domain so no sqrt core is needed, and
position-independent edge events (fabric can't know host position — the
host layer applies that). Session boundaries are HOST-commanded (TYPE
0x11/0x91, reusing symcfg's exact write-and-echo-is-the-ack pattern)
rather than computed in fabric — the host already knows the market
calendar; hardware calendar logic would just be new bug surface
replicating what Python already does correctly. First synthesis failed
timing (WNS -7.4 ns, wide arithmetic chained combinationally); fixed by
spreading the decision across one FSM state per operation and registering
every multiply's inputs/outputs for DSP48 MREG/PREG inference — ~5 extra
cycles out of a ~200,000-cycle budget, zero behavioral change.

**Host** (`tick_protocol.VWAPMirror`, `bridge.py`): the third independent
implementation of the same spec (RTL -> SV testbench model -> Python),
cross-checked bit-for-bit against the RTL simulation's own emitted values
before being trusted. `--selftest` (below) now exercises the session-reset
control path and all three engines in one deterministic run against real
hardware.

## Hardware acceptance test (run this after any bitstream change)
```bash
python3 host/order_manager.py --port /dev/ttyUSB1 --symbol SPY --selftest
```
No broker, no trading — a deterministic stimulus, checked against
independent host models bit-for-bit. Prints `PASS` or specific `DIAG`
lines. Also exercises the VWAP session-reset path (TYPE 0x11/0x91)
against real hardware, not just simulation.

## Replay REAL historical data against the real board
```bash
python3 host/order_manager.py --port /dev/ttyUSB1 --symbol QQQ \
    --source historical \
    --trades historical_trades/QQQ_2026-01-01_2026-04-01.trades.jsonl \
    --broker mock
```
The bring-up step between `--selftest` and a live/paper session:
`--selftest`'s stimulus is one hand-computed sequence; `--source sim` is a
synthetic random walk; this is the actual market data your backtests
scored, driving the actual board. `--trades` accepts comma-separated files
in chronological order (same convention `backtest.py` uses); `--replay-rate`
(default 200/s) paces the replay — the real recorded gaps between historical
ticks are NOT reproduced, since a full day's ticks at real spacing would
take hours; `--replay-max` (default 20,000) bounds how much of a
potentially enormous file one run touches. Single-symbol only. Refuses
outright if combined with `--live` — replaying already-happened prices
through a real broker would place real trades on stale data. Watch for
`RESULT: OK` and zero `diverged` counts in the session summary; add
`--dashboard PORT` to watch it visually (slow `--replay-rate` down to
~20 to actually follow it on screen).

## VWAP as the live strategy
```bash
python3 host/order_manager.py --port /dev/ttyUSB1 --symbol QQQ \
    --strategy vwap_bounce \
    --source alpaca --broker paper --dashboard 8000
```
`--strategy` now accepts `sma`, `ema`, or `vwap_bounce` — whichever is
picked trades for real (or on paper); the other two are scored in the
background under identical RiskPolicy clones, same relationship EMA has
always had to a live SMA, now generalized to three peers. This is a
DIFFERENT thing from the separate `--vwap-bounce` flag, which adds an
always-score-only HOST-computed VWAP row for comparison and can be used
alongside any `--strategy` choice.

Before trusting this with real capital: same gate as the historical
replay above, specifically with `--strategy vwap_bounce` — `RESULT: OK`,
zero divergence, on recent real data for the symbol you intend to trade —
*then* `--live` (see "Live Trading Enablement" above; the confirmation
banner and retyped phrase now include the strategy name too).

## Final test census
```
RTL:   850+1900+36+31+20+1236+22+22                = 4117
Host:  71+29+56+49+50+40+155+30+50+28+154+17       =  729
Total                                               = 4846 checks, 0 failures
```
