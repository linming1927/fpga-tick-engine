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

**v2.0** — runtime symbol configuration. Wire format v2 (6-byte symbols for
all S&P 500 tickers; 24/32-byte frames; TYPE 0x10 slot writes ACKed by 0x90
echoes). 8-slot register file + 8x both engines + priority encoders in
fabric; slot writes reset slot engine state. Per-symbol models, verifiers,
positions, costs, scorecards; multi-symbol sim + Alpaca sources; GUI slot
editor. 3030 checks / 0 failures. BITSTREAM REBUILD REQUIRED.
