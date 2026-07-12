# FPGA Tick Parser — Integration Layer (MANUAL.md §4)

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
        --broker alpaca --symbol SPY --qty 1 --max-shares 5
```

## Full-system status
```
RTL benches:  31 + 20 + 850 + 14 = 915 checks   (RX, TX, engine unit, engine e2e)
Host suites:  37 + 25            =  62 checks   (protocol/bridge, order manager)
```
Pipeline: Alpaca WebSocket -> host bridge -> UART -> tick_parser -> SMA
crossover engine -> priority TX -> verified 0x83 -> risk policy -> Alpaca
paper order. Optional remaining: ntfy.sh push notifications on fills.

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
    --broker alpaca --live --symbol SPY --qty 1 --max-shares 1 \
    --max-daily-loss 50 --household-income 185000
# ...then type: LIVE SPY
```

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
