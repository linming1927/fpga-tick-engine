# fpga-tick-engine

A tick-driven trading engine for US equities. It ingests live or
historical trade prints, computes session VWAP and its dispersion,
generates mean-reversion signals, and routes orders to Alpaca with a
risk overlay in front of them.

Two front ends share the same engine:

- **`order_manager.py`** — a live or paper trading session, with a
  browser dashboard.
- **`backtest.py`** — the same `OrderManager`, the same risk overlay,
  the same models, replayed over historical trade files at full CPU
  speed. It is not a parallel reimplementation, so a change to the
  trading logic applies to both automatically.

---

## Quick start

### Paper trading session

Run from `host/`:

```bash
python3 order_manager.py \
    --source alpaca --broker alpaca \
    --symbols TSLA,NVDA,RKLB,RIVN,SOFI \
    --strategy vwap_bounce \
    --stop-sigma-mult 3.0 --anchor-gate-tolerance 0.01 \
    --risk-per-trade 50 \
    --max-notional 3000 --max-position-notional 5000 \
    --max-daily-loss 3000 --cancel-stale-orders \
    --dashboard 8000 --household-income 185000 \
    --log ticks.jsonl --audit audit.jsonl
```

Dashboard at <http://localhost:8000>. `ALPACA_KEY` and `ALPACA_SECRET`
must be set in the environment; `--source alpaca` needs the
`websocket-client` package.

`--broker alpaca` alone is **paper** trading. Real money requires
`--live`, which additionally forces market-hours checks and demands
`--max-daily-loss`.

### Backtest

```bash
python3 backtest.py \
    --trades ../historical_trades/NVDA_2025-07-30_2026-07-29.trades.jsonl \
    --symbol NVDA --strategy vwap_bounce \
    --stop-sigma-mult 3.0 --anchor-gate-tolerance 0.01 \
    --risk-per-trade 50 \
    --max-notional 3000 --max-position-notional 5000 \
    --cooldown 60 --monthly \
    --audit /tmp/bt_audit.jsonl --killfile /tmp/bt.kill
```

Note the `../` — `backtest.py` lives in `host/` while
`historical_trades/` is a sibling directory. Comma-separate multiple
files for one symbol. `--max-daily-loss` is live-only and has no
backtest equivalent.

### Simulated session (no broker, no network)

```bash
python3 order_manager.py --symbols SPY,QQQ --source sim --broker mock \
    --strategy vwap_bounce --stop-sigma-mult 3.0 --risk-per-trade 50 \
    --n 40000 --rate 1200 --dashboard 8000 \
    --killfile /tmp/t.kill --audit /tmp/t.jsonl
```

---

## What the engine computes

Every accepted trade print updates three running session accumulators:
`Σv`, `Σp·v`, `Σp²·v`. From those:

```
VWAP     = Σpv / Σv
variance = Σppv/Σv − VWAP²          (clamped at 0)
σ        = sqrt(variance)
```

The band is `VWAP ± k·σ`, where `k` comes from `--vwap-k2-q8`
(256 = k of 1.0, since k = sqrt(k2_q8 / 256)). The band test is done
entirely in the squared domain — no square root — matching the original
RTL implementation exactly.

Accumulators reset once per trading day, on the first session started
that day. Restarting mid-day does **not** reset them, so VWAP stays
anchored to the real market open.

---

## The VWAP bounce strategy

Mean reversion around session VWAP, long only.

- **BUY** fires on a *bounce*: price was more than `k·σ` below VWAP and
  has come back up inside the band. The premise is that a stretched
  move away from the session's volume-weighted average tends to revert.
- **SELL** fires on the upward VWAP cross — price moving from below
  VWAP to at-or-above it.
- If one evaluation sees both edges, **SELL wins**.
- No signals fire until `--vwap-warmup` trades have been accepted, and
  the first warm evaluation primes the edge state without firing.

The engine emits **events, not positions** — it has no idea what you
hold. Position logic is applied downstream: sells only ever close an
existing position, and the strategy never shorts.

`--strategy sma` and `--strategy ema` (moving-average crossovers) are
also available and are scored in parallel for comparison, but
`vwap_bounce` is the one the risk overlay is built around.

---

## Risk management

The overlay is **off by default** and activates when
`--stop-sigma-mult` is greater than zero.

### Stop-loss

On a fresh entry, a stop is fixed at `VWAP − N·σ` using the session
VWAP *at that moment*, where N is `--stop-sigma-mult`. It never moves
afterwards — not when you add to the position, and not as VWAP drifts.
It is checked on **every tick**, independently of whether a strategy
signal happens to fire.

### Position sizing

```
shares = --risk-per-trade / (entry price − stop price)
```

The intent is that every position risks roughly the same dollar amount
if stopped out, regardless of the symbol's volatility — a calm stock
gets more shares, a volatile one fewer.

Note the interaction with the caps below: the risk formula only
determines size while its result fits under `--max-notional`. Once the
computed size exceeds that cap, the cap decides instead, and the
effective dollar risk becomes `2 × notional × σ%`, which scales with
volatility rather than staying flat.

### Caps, which trim rather than block

Three independent limits, applied in order:

| cap | limits |
|---|---|
| `--max-notional` | dollars in a **single order** |
| `--max-position-notional` | dollars in the **whole position** |
| `--max-shares` *(backtest only, effectively disabled)* | share count |

Each **reduces the order to what fits** rather than rejecting it. An
order is only blocked outright when there is no room left for even one
share. A trimmed order is printed and written to the audit log with
both the requested and filled quantity.

### Pyramiding

Adds to an open position are sized against the **original committed
stop**, never a freshly recomputed one — a stop that drifted down with
a declining market would defeat its own purpose. Total exposure is
bounded by `--max-position-notional`.

### The anchored-VWAP sell gate

A position that survives past the day it opened gets a second VWAP,
accumulated only since the position opened. Such a position can only be
sold at or above that anchor (within `--anchor-gate-tolerance`), the
idea being to avoid dumping an overnight hold into a bad print.

**The stop always overrides this gate.** Same-day positions are not
gated at all.

### Positions carried across a restart

A position reconciled from the broker at startup is adopted into the
overlay, with its true open date recovered from the audit log so the
sell gate treats it correctly. Its stop is deferred until that symbol's
session VWAP has both warmed up and shown real dispersion — computing
it against an empty mirror would produce a stop of exactly zero, which
could never fire.

### Orders that do not confirm

If an order does not confirm as filled within the poll window, it is
recorded as **pending** — the position and cost basis are left alone,
because the shares do not exist yet. Before the next order on that
symbol, the pending one is settled: booked if it filled (at the
broker's real price), cancelled if it is still working, forgotten if it
is gone. Partial fills book what actually filled.

### Wash-trade recovery

Alpaca rejects an order that opposes a still-live order on the same
symbol. Since the rejection names the conflicting order, that order is
cancelled and the submission retried once before the rejection counts
against the kill switch.

### Kill switch

Latches and stops all further orders. Trips on:

- three consecutive broker rejections
- model/hardware divergence (hardware mode)
- `--max-daily-loss` breached (realized P&L only)

It persists as a file on disk (`--killfile`); delete that file to
re-arm a future session. A rejection also starts the cooldown, so a
repeating conflict cannot burn the whole rejection budget in a second.

---

## `order_manager.py` flags

### Session and data source

| flag | default | description |
|---|---|---|
| `--symbols`, `--symbol` | `SPY` | comma-separated symbols to trade |
| `--source` | `sim` | `sim`, `alpaca` (live feed), or `historical` |
| `--broker` | `mock` | `mock` (nothing leaves the machine) or `alpaca` |
| `--live` | off | **real money.** Forces market hours, requires `--max-daily-loss` |
| `--trades` | — | trade file(s) for `--source historical`, comma-separated |
| `--replay-rate` | `200.0` | replay speed, trades/sec, for `--source historical` |
| `--replay-max` | `20000` | cap on replayed trades; 0 for no cap |
| `--n` | `200` | number of ticks for `--source sim` |
| `--rate` | `10.0` | ticks/sec for `--source sim` |
| `--start-price` | `500.0` | starting price for `--source sim` |
| `--relay-url` | — | alternate websocket endpoint instead of Alpaca's |

### Strategy

| flag | default | description |
|---|---|---|
| `--strategy` | `sma` | which strategy actually trades: `sma`, `ema`, `vwap_bounce` |
| `--fast` | `8` | SMA fast window |
| `--slow` | `32` | SMA slow window |
| `--ema-kf` | `3` | EMA fast smoothing shift |
| `--ema-ks` | `5` | EMA slow smoothing shift |
| `--vwap-warmup` | `20` | trades before VWAP signals are trusted |
| `--vwap-k2-q8` | `256` | band width as k², Q8 fixed point (256 = k of 1.0) |
| `--force-vwap-reset` | off | reset session VWAP even if already reset today |
| `--vwap-bounce` | off | score VWAP bounce alongside without trading it |
| `--vwap-band-k` | `1.0` | band width for that scored-only row |
| `--profit-gate` | off | score a profit-gated SMA variant alongside |
| `--pg-max-hold-days` | `5.0` | max hold for the profit-gated row |
| `--ladder` | off | enable the standalone ladder strategy |
| `--ladder-step` | `0.03` | ladder rung spacing |
| `--ladder-levels` | `3` | number of ladder rungs |
| `--ladder-qty` | `1` | shares per ladder rung |
| `--ladder-method` | `week_vwap` | ladder anchor basis |
| `--ladder-baseline` | — | explicit ladder baseline price |

### Risk

| flag | default | description |
|---|---|---|
| `--stop-sigma-mult` | `0.0` | stop at VWAP − N·σ. **0 disables the whole overlay** |
| `--risk-per-trade` | `500.0` | dollars risked per fresh entry |
| `--anchor-gate-tolerance` | `0.0` | how far below its anchored VWAP an older position may still sell |
| `--qty` | `5` | shares per order when not risk-sized |
| `--max-notional` | `3000.0` | dollar cap per single order |
| `--max-position-notional` | `10000.0` | dollar cap on a whole position |
| `--max-orders-per-day` | `1000` | daily order count cap |
| `--cooldown` | `60.0` | minimum seconds between orders on a symbol |
| `--max-daily-loss` | — | realized-loss dollars that halt the session |
| `--ignore-market-hours` | off | skip the market-hours gate (ignored under `--live`) |
| `--cancel-stale-orders` | off | cancel leftover open orders at startup |
| `--killfile` | `om.kill` | kill-switch marker file |

### Output

| flag | default | description |
|---|---|---|
| `--audit` | `om_audit.jsonl` | audit log: fills, blocks, halts, cost basis |
| `--log` | — | raw tick log |
| `--dashboard` | — | port for the browser dashboard |
| `--household-income` | — | enables the tax estimate |
| `--filing-status` | `mfj` | `single` or `mfj` |
| `--state-rate` | `4.4` | state income tax rate, percent |
| `--gross` | off | report gross P&L instead of net |
| `--no-timestamps` | off | drop timestamps from console output |

### Hardware mode

| flag | default | description |
|---|---|---|
| `--port` | — | serial device. Omit for the direct in-process engine |
| `--baud` | `921600` | serial baud rate |
| `--verify-grace-s` | `2.0` | grace window for fabric-vs-model verification |
| `--selftest` | off | hardware acceptance test; requires `--port` |

---

## `backtest.py` flags

`--trades` and `--symbol` are required.

| flag | default | description |
|---|---|---|
| `--trades` | *required* | trade file(s), comma-separated |
| `--symbol` | *required* | symbol to backtest |
| `--strategy` | `sma` | which strategy trades: `sma`, `ema`, `vwap_bounce` |
| `--fast` | `8` | SMA fast window |
| `--slow` | `32` | SMA slow window |
| `--ema-kf` | `3` | EMA fast smoothing shift |
| `--ema-ks` | `5` | EMA slow smoothing shift |
| `--vwap-warmup` | `20` | trades before VWAP signals are trusted |
| `--vwap-k2-q8` | `256` | band width as k², Q8 (256 = k of 1.0) |
| `--stop-sigma-mult` | `0.0` | stop at VWAP − N·σ. 0 disables the overlay |
| `--risk-per-trade` | `500.0` | dollars risked per fresh entry |
| `--anchor-gate-tolerance` | `0.0` | sell-gate tolerance for older positions |
| `--qty` | `5` | shares per order when not risk-sized |
| `--max-notional` | `3000.0` | dollar cap per single order |
| `--max-position-notional` | `10000.0` | dollar cap on a whole position |
| `--max-orders-per-day` | `1000` | daily order count cap |
| `--cooldown` | `60.0` | minimum seconds between orders |
| `--audit` | `backtest_audit.jsonl` | audit log for the backtest run |
| `--killfile` | `backtest.kill` | kill-switch marker file |
| `--monthly` | off | print and save a per-month P&L breakdown |
| `--results-dir` | `backtest_results/` | where saved runs go |
| `--no-save` | off | do not save results to disk |
| `--vwap-bounce` | off | score VWAP bounce alongside |
| `--vwap-band-k` | `1.0` | band width for that scored row |
| `--profit-gate` | off | score a profit-gated SMA variant alongside |
| `--pg-max-hold-days` | `5.0` | max hold for the profit-gated row |
| `--htf-ltf` | off | score a high/low timeframe variant |
| `--htf-interval` | `3600` | higher timeframe, seconds |
| `--ltf-interval` | `300` | lower timeframe, seconds |
| `--blended` | off | score a blended multi-sleeve strategy |
| `--blend-vwap-shares` | `6` | VWAP sleeve share cap |
| `--blend-vwap-notional` | `1300.0` | VWAP sleeve dollar cap |
| `--blend-pg-shares` | `4` | profit-gated sleeve share cap |
| `--blend-pg-notional` | `700.0` | profit-gated sleeve dollar cap |
| `--blend-account-notional` | `2000.0` | blended account dollar cap |

---

## Supporting tools

**`analyze_sigma_distribution.py`** — the historical distribution of σ
as a percentage of price, for choosing risk parameters against real
data rather than guesswork:

```bash
python3 analyze_sigma_distribution.py \
    --trades ../historical_trades/NVDA_....trades.jsonl --symbol NVDA
```

The low percentiles matter most: they show how tight σ gets on the
quietest real moments, which is where position sizing misbehaves.

**`list_backtest_results.py`** — browse saved backtest runs.

**`fetch_historical_trades.py`** — download trade history from Alpaca.

---

## Hardware mode

The project began as a host for an Arty A7-100T FPGA running the same
VWAP engine in RTL. That path still works: pass `--port` and the engine
talks to the board (or to `fpga_emulator.py` standing in for one) over
UART, verifying every fabric signal against the host model.

```bash
# terminal 1
python3 fpga_emulator.py --symbol SPY

# terminal 2
python3 order_manager.py --port /tmp/fpga-tick-emulator --baud 115200 ...
```

Against real silicon this verification is meaningful, since the RTL and
the Python models are genuinely independent implementations. Against
the emulator it is not — the emulator imports the same model classes
the host checks against — which is why the direct in-process engine is
the default.

---

## Layout

```
host/
  order_manager.py    live/paper session, risk overlay, broker, kill switch
  backtest.py         historical replay through the same OrderManager
  tick_engine.py      direct in-process engine (the default)
  bridge.py           UART engine for real hardware (--port)
  feeds.py            sim / historical / Alpaca data sources
  tick_protocol.py    SMA, EMA and VWAP models; wire framing
  position_risk.py    stops, anchored VWAP, risk sizing, sell gate
  costs.py            cost basis, fees, realized P&L, tax estimate
  compare.py          strategy scorecards and comparison reports
  dashboard.py        browser console
rtl/                  SystemVerilog VWAP engine
historical_trades/    downloaded trade history
```

## Tests

```bash
cd host
rm -f om.kill
for t in test_*.py; do python3 "$t"; done
```

Each file prints its own `RESULT: n PASS / n FAIL`.
