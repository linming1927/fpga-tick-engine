# Code Walkthrough: FPGA Tick Parser Integration Layer

*A learning-oriented explanation of every file — what the code does, and why it was written that way.*

This document is the companion to MANUAL.md. Where the manual specifies *what* the system does (wire format, port lists, test cases), this walkthrough explains *how* the RTL achieves it and the reasoning behind each design decision. It assumes you can read SystemVerilog but doesn't assume you've internalized FPGA design idioms yet — every recurring pattern is explained the first time it appears, and cross-referenced after that.

---

## 1. The Big Picture

Five source files form the design, one file constrains it to the board, one file tests it:

```
rtl/uart_rx.sv          serial bits  → bytes
rtl/tick_parser.sv      bytes        → decoded tick frames
rtl/timestamp_us.sv     wall clock   → per-frame arrival timestamp
rtl/top_arty.sv         wires the three together + LEDs + debug
constraints/top_arty.xdc  maps ports to physical pins, defines the clock
sim/tb_top_integration.sv  drives real UART waveforms, checks everything
```

Everything runs on **one clock** (the Arty's 100 MHz oscillator) and **one reset**. This is deliberate. Multi-clock designs require clock domain crossing (CDC) machinery — synchronizers, async FIFOs, gray-coded pointers — and every CDC boundary is a place where a design can fail intermittently in ways simulation won't catch. This design has exactly one asynchronous input (the UART RX line itself), and it is tamed at the point of entry with a two-flip-flop synchronizer. Past that first pair of flops, every signal in the system is synchronous to the same clock, which means standard static timing analysis can *prove* the design correct against setup/hold requirements. That provability is the whole reason this project uses an FPGA: determinism you can verify, not speed.

The second system-wide principle is the **single-cycle pulse handshake**. Modules communicate readiness with strobes that are high for exactly one clock cycle: `rx_valid`, `msg_valid`, `parse_error`, `sof_seen`. Compare this to a level-based handshake (valid stays high until acknowledged): pulses need no acknowledge wire, can't be double-counted by a well-behaved consumer, and compose trivially — a downstream module just does `if (pulse) ...` in its clocked block. The cost is that the consumer must be clocked at the same rate (it is — one clock, see above) and must never be "busy" when a pulse arrives. That second condition holds here because of a huge rate mismatch that shapes the entire design: at 115 200 baud, one byte takes ~87 µs = ~8 680 clock cycles to arrive. The parser does one cycle of work per byte. It is idle 99.99 % of the time, so nothing downstream ever needs to say "wait."

---

## 2. Idioms You'll See in Every File

These patterns repeat throughout the RTL. Understanding them once saves re-explaining them in every section.

### 2.1 `always_ff` and the synchronous process

```systemverilog
always_ff @(posedge clk) begin
    if (!rst_n) begin
        ...reset values...
    end else begin
        ...normal operation...
    end
end
```

`always_ff` is the SystemVerilog-specific version of `always` that tells both the compiler and the human: *everything assigned in this block is a flip-flop*. If you accidentally write code that would infer a latch or combinational logic inside it, the tool errors out instead of silently building the wrong hardware. This is one of the main reasons to prefer SystemVerilog over Verilog-2001 for new RTL.

All assignments inside use `<=` (non-blocking). Non-blocking assignment means "sample the right-hand side now, update the left-hand side at the end of the time step" — which is exactly how real flip-flops behave: they all sample their D inputs on the clock edge simultaneously, using *pre-edge* values. If you used blocking `=` here, statement order would change behavior and simulation would diverge from synthesized hardware.

### 2.2 Synchronous, active-low reset (`rst_n`)

The reset check is *inside* the clocked block and not in the sensitivity list, making it a **synchronous reset**: the registers reset on a clock edge, not asynchronously the instant reset asserts. Xilinx 7-series fabric slightly prefers synchronous resets (the flip-flop's dedicated set/reset pin is synchronous; async resets consume routing and can cause reset-release timing problems). Active-low (`_n` suffix) is a convention inherited from board-level design, where reset lines idle high — and indeed the Arty's reset button is electrically active-low, so the convention flows naturally from pin to RTL.

### 2.3 The default-assignment pulse pattern

```systemverilog
rx_valid <= 1'b0;      // default: pulse is low...
...
if (...) rx_valid <= 1'b1;   // ...unless this cycle earns it
```

Assigning the strobe low at the top of the block, then conditionally overriding it, guarantees the pulse lasts exactly one cycle with no explicit "clear it next cycle" bookkeeping. In a clocked block, the *last* assignment to a signal wins, so the override works. This is the standard way to generate clean single-cycle pulses and you'll see it in `uart_rx`, `tick_parser`, and the testbench monitors.

### 2.4 `unique case`

`unique case (state)` asserts to synthesis (and simulation) that exactly one branch matches — no overlaps, no fall-through. Vivado uses this to build parallel, priority-free decode logic (a mux rather than a priority chain), and simulators flag a runtime warning if the claim is ever violated. Icarus Verilog prints a "unique qualities are ignored" note because it doesn't implement the check — harmless; the keyword is there for Vivado's benefit.

### 2.5 `` `default_nettype none ``

By default, Verilog silently creates a 1-bit wire when you use an undeclared identifier — historically the source of countless bugs where a typo'd port name becomes a floating wire and the design "works" in some simulations and fails on hardware. `` `default_nettype none `` turns that into a compile error. The matching `` `default_nettype wire `` at the end of each file restores the default so the setting doesn't leak into other files compiled afterward (compiler directives are global and file-order-dependent — an ugly language wart worth knowing about).

### 2.6 `$clog2` for counter sizing

```systemverilog
localparam int CNT_W = $clog2(CLKS_PER_BIT);
logic [CNT_W-1:0] clk_cnt;
```

`$clog2(N)` returns the number of bits needed to count up to N−1. Sizing counters from parameters this way means the module stays correct if you change `CLK_HZ` or `BAUD` — the counter automatically grows or shrinks. Hard-coding `logic [9:0]` would work for the default parameters and silently break for others.

### 2.7 Initial values on registers (`state_t state = S_IDLE;`)

On Xilinx FPGAs, every flip-flop's power-up value is programmed into the bitstream, so an initializer like `= S_IDLE` is real, synthesizable hardware behavior — not just a simulation convenience. (This is an FPGA luxury; in ASIC design initializers do nothing and you must rely on reset alone.) Here they serve both purposes: the hardware comes up in a known state even before the reset sequencer finishes, and simulation avoids X (unknown) propagation at time zero. The X-propagation issue is worth understanding: before the first reset, an uninitialized `state` is X in simulation, X matches no case branch, and `unique case` complains. The initializer eliminates the whole class of problem.

---
## 3. `rtl/uart_rx.sv` — Turning an Asynchronous Bit Stream into Bytes

### 3.1 The problem UART reception actually poses

UART has no clock wire. The transmitter and receiver each run their own oscillator and merely *agree* on a nominal baud rate. The receiver must therefore recover timing from the data itself: it watches for the falling edge that begins the start bit, and from that single reference point, it predicts where the centers of the next nine bits (8 data + 1 stop) will fall. Everything in this module serves that one goal — sampling each bit as close to its center as possible, where the signal is stable and farthest from the edges.

### 3.2 The timing budget: why 868 is good enough

```systemverilog
localparam int CLKS_PER_BIT = CLK_HZ / BAUD;   // 100_000_000 / 115_200 = 868
```

The exact ratio is 868.055…, and integer division truncates to 868 — an error of 0.0064 % per bit. Errors accumulate across a frame because all sample points are predicted from the single start-bit edge: by the last sample (the stop bit, 9.5 bit-periods after the edge), the accumulated drift is 9.5 × 0.055 ≈ 0.5 clock cycles out of 8 680 — about 0.006 % of a bit period. The generally quoted tolerance for UART is that total error (both sides' oscillator inaccuracy plus quantization) must stay under roughly ±5 % of a bit period at the last sample point so you never sample outside the intended bit cell. We're using ~0.006 % of that budget; the FTDI chip's crystal on the other side is typically ±0.25 %. Comfortable margin — this is why the manual calls the 0.03 % figure negligible, and why no fractional baud-rate generator (a more complex accumulator-based divider) is needed.

### 3.3 The two-flip-flop synchronizer

```systemverilog
logic rx_meta, rx_sync;
always_ff @(posedge clk) begin
    rx_meta <= rx;
    rx_sync <= rx_meta;
end
```

The RX line comes from another clock domain (the host's), so its edges can land at any time relative to our clock — including *inside* the setup/hold window of the first flip-flop. When that happens the flop can go **metastable**: its output hovers at an invalid voltage for an unbounded (but exponentially unlikely to be long) time before resolving to 0 or 1. The first flop, `rx_meta`, absorbs this: its only job is to be allowed to go metastable. The second flop, `rx_sync`, samples `rx_meta` a full clock period later, by which time the metastability has resolved with overwhelming probability (MTBF for a 2-FF synchronizer at these speeds is measured in millennia). Only `rx_sync` is ever used by the FSM. **Rule: never let raw asynchronous signals touch your logic; never use the middle flop of a synchronizer for anything.**

Note the synchronizer has no reset — deliberately. Resetting it adds a load on the reset net for zero benefit; the pipeline flushes itself in two cycles regardless.

### 3.4 Walking the four states

**S_IDLE** waits for `rx_sync == 0`. The UART line idles high, so the first low sample is the leading edge of a start bit. Counters are held at zero here so each byte starts from a clean slate.

**S_START — the half-bit trick.** The FSM waits `CLKS_PER_BIT/2` cycles, then re-samples. Two things are accomplished at once. First, *glitch rejection*: a noise spike shorter than half a bit will have passed, the line will be high again, and the FSM returns to IDLE having ignored it — a legitimate start bit is still low at its midpoint. Second, *phase alignment*: after this half-bit wait we are standing at the **center** of the start bit, so every subsequent wait of one *full* bit period lands at the center of the next bit. One half-bit delay up front buys centered sampling for the entire byte. This is the classic software-UART/hardware-UART receiver structure and the single most important idea in the module.

**S_DATA — LSB first, by shifting right.**

```systemverilog
shift_reg <= { rx_sync, shift_reg[7:1] };
```

UART sends bit 0 of the byte first. Each new bit therefore belongs at the *top* of the register while everything received so far slides down — a right shift with the new bit inserted at bit 7. After eight samples, bit 0 (received first) has migrated to position 0 and bit 7 (received last) sits where it was inserted. First time readers often expect a left shift here; the right shift is exactly the LSB-first convention made concrete. (Contrast with the tick parser in §4, which assembles *big-endian multi-byte fields* and shifts **left** — the two shift directions correspond to the two orderings, and seeing why is a good self-test of understanding.)

The 3-bit `bit_idx` counter counts 0–7; when it hits 7 the state advances instead of the counter.

**S_STOP — validation, not just delay.** One more full bit period lands at the center of the stop bit. A stop bit must be high; sampling high means the byte framed correctly and `rx_data`/`rx_valid` are driven. Sampling low means the timing chain broke down somewhere — wrong baud rate, noise, a break condition — and the module pulses `rx_error` while leaving `rx_data` *untouched*. That last detail matters: downstream logic holding the previous byte never sees garbage. Returning to IDLE mid-stop-bit is safe because the line is (supposed to be) high, i.e., indistinguishable from idle; if a new start bit follows immediately, IDLE will catch its falling edge.

### 3.5 What was deliberately left out

A production UART might add 16× oversampling with majority voting per bit (better noise immunity), parity support, or a receive FIFO. None earn their complexity here: the "channel" is a 30 cm USB cable driving an FTDI chip, byte spacing is enormous relative to processing time, and the tick parser provides frame-level error detection anyway (SOF/EOF sentinels). Engineering judgment in RTL is mostly about what *not* to build.

---

## 4. `rtl/tick_parser.sv` — Bytes into Frames

### 4.1 Architecture: an FSM shaped exactly like the wire format

The parser is a direct transcription of the frame layout into states: `S_IDLE → S_TYPE → S_SYMBOL(×4) → S_PRICE(×4) → S_QTY(×2) → S_SIDE → S_TSTAMP(×8) → S_EOF → S_IDLE`. Because the frame is **fixed-length**, the FSM needs no length field, no escape sequences, no dynamic offsets — a state plus a small byte counter fully determines what the next byte means. This is *the* payoff of the fixed-length wire format decision in MANUAL.md §2, and it's what makes the parser small enough to reason about exhaustively (and, later, formally verify if you want a portfolio exercise: the state space is tiny).

The entire FSM body sits inside `if (rx_valid)`. Between bytes — those ~8 680 idle cycles — the parser does literally nothing: no state changes, no counter activity. This makes the design's behavior a pure function of the byte sequence, which is why the Python behavioral model (which knows nothing about clocks) can mirror it exactly.

### 4.2 Big-endian assembly by left shift

```systemverilog
r_price <= { r_price[23:0], rx_data };
```

Multi-byte fields arrive most-significant-byte first (big-endian, a.k.a. network byte order). The concatenation `{old[23:0], new_byte}` discards the top 8 bits, slides everything up, and appends the new byte at the bottom. After 4 bytes, the first byte received — the most significant — has been pushed to the top, which is exactly where big-endian says it belongs. The manual's `AAPL` example traces this cycle by cycle. One register, one line of code, no byte-lane multiplexers: this is why the wire format chose big-endian. (Little-endian would need `{new_byte, old[31:8]}` — equally cheap in hardware, but big-endian matches how `struct.pack('>...')` and network protocols write things, so the host side stays simple too.)

Note the accumulators are shared-nothing: `r_symbol`, `r_price`, etc. are separate registers, each only written in its own state. A tempting "optimization" — one big 160-bit shift register for the whole frame — would work but would smear all the fields together, make the code unreadable, and save nothing (the registers exist either way).

### 4.3 The double-buffered output: why `r_*` and the outputs are separate registers

This is the most important design decision in the file. The parser accumulates into *working* registers (`r_type`, `r_symbol`, …) and only copies them to the *output* ports (`msg_type`, `symbol`, …) in `S_EOF`, and only if the EOF byte checks out:

```systemverilog
if (rx_data == EOF_BYTE) begin
    msg_type <= r_type;  symbol <= r_symbol;  ...
    msg_valid <= 1'b1;
end else begin
    parse_error <= 1'b1;      // outputs untouched
end
```

Consequences, in increasing order of subtlety:

The outputs are **transactionally consistent** — they always describe one complete, validated frame, never a mixture of an old frame and a half-assembled new one. Downstream logic (the indicator engine) may combinationally read `price` at any time without qualifying it against `msg_valid`; the value is always *a* real price from *a* real frame. `msg_valid` then means "the outputs just changed," an edge notification rather than a data qualifier. This decouples consumers that care about *events* (count ticks, latch on update) from consumers that care about *state* (what's the current price).

A corrupted frame is **invisible** in the data path. `parse_error` fires, but every output register still holds the last good frame — the failure mode is "you don't get the new tick," never "you get a garbled tick." For a system that will eventually gate trading decisions, silently-wrong data is the worst possible failure; this structure makes it impossible by construction.

And `msg_valid`/`parse_error` are **mutually exclusive by construction**, not by checking: they're assigned in the two branches of one if/else, so no test needs to verify all four combinations — three of them can't be expressed.

### 4.4 Resynchronization for free

If line noise or a host bug misaligns the stream, what happens? S_IDLE ignores every byte that isn't 0xAA — so garbage between frames is simply skipped. The nastier case is 0xAA *appearing inside garbage*: the parser dutifully starts assembling a spurious frame, consumes the next 20 bytes, and then — with probability 255/256 — sees a wrong EOF, fires `parse_error`, and returns to IDLE, resynchronized. The design accepts a bounded, detectable disturbance (one lost frame's worth of bytes) instead of adding heavier machinery (checksums, byte-stuffing, sync hunting across a window). The Python model discovered the practical implication documented in MANUAL.md: after an error, the host shouldn't try to be clever — just send the next complete frame.

There is a residual 1/256 case worth knowing about: if the garbage byte landing in the EOF slot happens to *be* 0x55, a spurious frame is accepted as valid. The SOF+EOF sentinels are a lightweight integrity check, not a cryptographic one. If this ever mattered (it doesn't, over a USB cable), the fix is a CRC byte in the wire format — a good future exercise, and exactly the kind of trade-off (error detection strength vs. format complexity) interviewers like to probe.

### 4.5 `sof_seen` — the one addition beyond the manual's spec

```systemverilog
S_IDLE: if (rx_data == SOF_BYTE) begin
    state <= S_TYPE;  sof_seen <= 1'b1;
end
```

The integration checklist wants the arrival timestamp latched "on `rx_valid` at the first byte of each frame (the SOF byte)." The naive implementation — latch whenever `rx_valid && rx_data == 8'hAA` in the top level — is wrong in a subtle way: 0xAA is a perfectly legal *data* byte (e.g., inside PRICE or TSTAMP), and the naive rule would re-latch the timestamp mid-frame, corrupting the arrival time. Only the parser knows whether a given 0xAA is *acting as* an SOF, because only the parser knows it's in S_IDLE. So the parser exports that one bit of knowledge as a pulse. General lesson: **put detection where the context lives**, then export a clean strobe, rather than duplicating (and inevitably diverging from) the FSM's knowledge elsewhere.

---

## 5. `rtl/timestamp_us.sv` — Time, and When to Trust It

### 5.1 The microsecond tick

```systemverilog
localparam int CLKS_PER_US = CLK_HZ / 1_000_000;   // 100
if (div_cnt == CLKS_PER_US - 1) begin
    div_cnt <= '0;
    us_now  <= us_now + 64'd1;
end
```

A 100-cycle divider carves the 100 MHz clock into microseconds. Why not just count clock cycles and divide later? A 64-bit cycle counter would also never overflow, but every consumer would carry a ×100 conversion, and the host-side comparison against `host_tstamp` (already in µs) is cleaner when both sides speak the same unit. Microsecond resolution is also *honest*: UART delivery quantizes arrival to ~87 µs per byte anyway, so nanosecond precision would be false precision.

The 64-bit width is not extravagance. A 32-bit µs counter wraps in 71.6 minutes — a system that misbehaves only after an hour of uptime is a classic field-failure generator. 64 bits wrap in ~584 000 years; the correct amount of rollover-handling code is therefore *none*, and the absence of that code is itself a simplification worth the 32 extra flip-flops.

### 5.2 Pending/commit: making the timestamp obey the same contract as the data

```systemverilog
if (sof_seen)  pending    <= us_now;
if (msg_valid) arrival_us <= pending;
```

Why two stages? Suppose `arrival_us` latched directly on `sof_seen`. Then a *corrupted* frame — or a spurious 0xAA in garbage — would update the arrival timestamp even though no valid data followed, and `arrival_us` would disagree with the parser outputs (which, per §4.3, still hold the previous good frame). The pending/commit pair mirrors the parser's own working-vs-output register structure: `pending` is speculative ("a frame *may* be starting; note the time"), and only a good EOF — signaled by `msg_valid` — promotes it. A bad frame's pending value is silently overwritten by the next SOF. The result is a system-wide invariant: **on every `msg_valid` pulse, `{msg_type, symbol, price, qty, side, host_tstamp, arrival_us}` all describe the same frame**, and between pulses they all hold steady together. Timestamps are metadata; metadata should travel under the same transactional rules as the data it describes.

### 5.3 The epoch caveat

`us_now` counts from FPGA reset, while `host_tstamp` is Unix time — the two share a *unit* but not an *epoch*, and their oscillators drift relative to each other (crystal tolerance ~tens of ppm). So `arrival_us − host_tstamp` is not directly the transit latency; it's latency plus a large constant offset plus slow drift. The host bridge should either calibrate the offset once at startup (send a frame, compare) and track it, or work with frame-to-frame *deltas*, where the constant cancels. This is documented in the module header because it's the kind of thing that bites six weeks later when someone (you) reads a "latency" of 1.75 billion seconds.

---
## 6. `rtl/top_arty.sv` — Integration, and the Unglamorous Parts That Make Hardware Work

A top level looks trivial — "just wires" — but three of its subsystems (reset conditioning, debug visibility, human-visible indicators) are exactly the things that separate RTL that simulates from RTL that brings up smoothly on a bench.

### 6.1 Reset conditioning

```systemverilog
always_ff @(posedge clk100) begin
    rst_meta <= ck_rst;
    rst_sync <= rst_meta;
    if (!rst_sync)               begin rst_cnt <= '0;  rst_n <= 1'b0; end
    else if (rst_cnt != 4'hF)    begin rst_cnt <= rst_cnt + 1'b1; rst_n <= 1'b0; end
    else                               rst_n <= 1'b1;
end
```

The raw button is asynchronous (so it gets the same 2-FF treatment as the UART line — see §3.3) and mechanically bouncy. The 16-cycle hold-off after release does two jobs. It swallows contact bounce: rst_n won't wiggle as the button contacts chatter, because any re-assertion of `!rst_sync` restarts the count. More importantly, it guarantees a **synchronous, simultaneous reset release** to every module: since `rst_n` is itself a registered signal on `clk100`, all modules leave reset on the same clock edge. Asynchronous reset *release* is a real hazard even in synchronous-reset designs at higher complexity — different flops seeing the release on different edges can start an FSM in an inconsistent state. Building the habit of conditioned resets now costs one small always block.

### 6.2 The instantiations — reading a netlist in text form

The three module instances use **named port connections** (`.clk(clk100)`) rather than positional ones. Positional connection (`uart_rx u1 (clk100, rst_n, uart_txd_in, ...)`) breaks silently when a module's port list is reordered; named connection breaks *loudly* (compile error) or not at all. In aerospace code review, positional port connection on anything beyond two ports is typically an automatic finding. The parameter overrides `.CLK_HZ(CLK_HZ)`, `.BAUD(BAUD)` push the top-level parameters down, so the whole design retargets (say, to a 50 MHz clock or 921 600 baud) by editing exactly one place — checklist item 1 is satisfied by construction rather than by matching magic numbers.

### 6.3 The indicator-engine boundary: re-registering and `mark_debug`

```systemverilog
(* mark_debug = "true" *) logic [31:0] tick_price;
...
tick_valid <= msg_valid;
if (msg_valid) begin
    tick_price <= price;  ...
    tick_fpga_ts <= arrival_us;
end
```

Checklist item 4 asks that the decoded fields be "routed to the indicator engine." With no engine yet, the honest implementations are (a) dangling wires — which synthesis would optimize away entirely, leaving nothing to probe on hardware — or (b) this: re-register the frame into a named `tick_*` bus at the boundary where the engine will attach. Choice (b) buys three things.

First, **pipelining discipline**: `tick_valid` is `msg_valid` delayed one cycle, and the fields are captured on the same edge, so pulse and data stay aligned — the consumer sees the classic valid+payload interface with one clean cycle of registration between subsystems. Registering at block boundaries is the habit that keeps timing closure easy as designs grow; a one-cycle latency costs 10 ns against an 87 µs byte time, i.e., nothing.

Second, `(* mark_debug = "true" *)` tells Vivado two things: *don't optimize these nets away* even though nothing consumes them yet, and *make them available to attach an Integrated Logic Analyzer (ILA) core* post-synthesis, from the GUI, without editing RTL. The ILA is a logic analyzer built from spare FPGA resources — block RAM for sample storage, fabric for triggers — that streams captures back over JTAG. With these attributes in place, "show me the last 1024 decoded ticks, triggered on parse_error" is a five-minute setup on the bench. Debug visibility that's designed in beforehand is cheap; retrofitted afterward it costs a re-synthesis per experiment.

Third, the commented-out `indicator_engine` instantiation template documents the intended interface *in the language that will consume it* — better than prose, because when you write the engine, the port list is already negotiated.

The same `mark_debug` treatment goes on `parse_error_count`, the 16-bit **saturating** error counter (checklist item 5's "counter register readable over a debug interface" — ILA is that interface for now). Saturation (`!= 16'hFFFF` guard) instead of wraparound: a counter stuck at FFFF says "many errors" unambiguously, while a wrapped counter reading 3 lies to you.

### 6.4 Pulse stretchers and the heartbeat — interfacing to human eyeballs

```systemverilog
str_perr <= parse_error ? '1 : (str_perr != 0 ? str_perr - 1'b1 : '0);
assign led[0] = (str_perr != 0);
```

A one-cycle pulse is 10 ns of light — five orders of magnitude below flicker-fusion perception. The stretcher is a down-counter: any pulse reloads it to all-ones (`'1` fills the 25-bit width), and it counts down to zero; the LED is on while nonzero. 2²⁵ cycles at 100 MHz ≈ 0.34 s — a visible blink. A retriggering event holds the LED on, which conveniently makes *error rate* visible too: occasional blink = occasional error, solid = something's badly wrong.

`led[3]`, the heartbeat, is bit 26 of a free-running counter — a ~0.75 Hz square wave costing one counter. Its diagnostic value is real: on the bench, a blinking heartbeat proves at a glance that the bitstream loaded, the clock oscillates, the reset released, and the constraints placed the LED — before you send a single UART byte. Every FPGA board bring-up should start with a heartbeat for the same reason every embedded bring-up starts with a blinking LED.

The LED map layers the diagnostics: LED3 = design alive, LED2 = bytes arriving but framing broken (baud mismatch → check host config), LED0 = bytes fine but frames broken (wire-format mismatch → check the bridge's `struct.pack`), LED1 = everything working. Each LED isolates one layer of the stack — a deliberate debugging ladder, not four random indicators.

---

## 7. `constraints/top_arty.xdc` — Where RTL Meets Physics

Synthesis turns RTL into a netlist of LUTs and flip-flops, but nothing in the RTL says *which pin* `uart_txd_in` is or *how fast* `clk100` runs. The XDC (Xilinx Design Constraints — Tcl syntax) supplies the physical and temporal ground truth.

### 7.1 Pins and I/O standards

```tcl
set_property -dict { PACKAGE_PIN E3  IOSTANDARD LVCMOS33 } [get_ports { clk100 }]
```

Each port gets a package pin (from the Arty schematic, via Digilent's master XDC) and an I/O standard. `LVCMOS33` sets the I/O buffer's voltage levels and drive characteristics to 3.3 V CMOS — it must match what the board wires to that bank, or you get anything from marginal logic levels to damaged transceivers. Vivado *errors out* if any port lacks these two properties; that's a feature, not an obstacle.

The pin-naming correction from the README bears repeating because it's a classic trap: Digilent names the UART pins from the *host FTDI's* perspective. `uart_txd_in` (A9) is where the host **t**ransmits — therefore where the FPGA *receives*. `uart_rxd_out` (D10) is the FPGA→host direction, unused until the TX layer exists. Signal names at interfaces always carry a point of view; the first question to ask of any `TX`/`RX` label is *whose* TX.

### 7.2 The clock constraint — the line that makes timing analysis mean something

```tcl
create_clock -add -name sys_clk -period 10.000 -waveform {0 5} [get_ports { clk100 }]
```

This declares a 10 ns (100 MHz) clock at the port. From this single fact, static timing analysis (STA) derives a setup/hold requirement for **every register-to-register path in the design** and proves each one meets it (or reports which don't, and by how much). Without a clock constraint, Vivado has no requirements to check — the design will "pass timing" vacuously and may or may not work. The first thing to check in any unfamiliar FPGA project is whether the clocks are actually constrained.

### 7.3 False paths — telling STA the truth about asynchronous inputs

```tcl
set_false_path -from [get_ports { uart_txd_in }]
set_false_path -from [get_ports { ck_rst }]
set_false_path -to   [get_ports { led[*] }]
```

STA assumes every path is synchronous unless told otherwise. But `uart_txd_in` has *no defined timing relationship* to `clk100` — that's what asynchronous means — so any setup/hold analysis of it is meaningless, and Vivado would either fabricate a constraint or fail the path spuriously. `set_false_path` says: don't analyze this; it's handled structurally. The crucial discipline is that a false path is a *claim*, and the RTL must make the claim true — here, the 2-FF synchronizers are the structural handling. Cutting a path in the XDC without a synchronizer in the RTL is how you build a design that passes timing and fails in the field at random intervals. The LED false paths are the trivial case: a human eye has no setup requirement.

### 7.4 Bitstream configuration

```tcl
set_property CFGBVS VCCO        [current_design]
set_property CONFIG_VOLTAGE 3.3 [current_design]
```

These describe how the configuration bank is powered on the Arty (3.3 V, per Digilent's documentation). Omitting them gets you a warning and, in some flows, a bitstream the board refuses. Cargo-culted in every Arty project for good reason.

---

## 8. `sim/tb_top_integration.sv` — The Testbench as an Instrument

### 8.1 What "bit-level" buys over the byte-level testbench

The manual's original `tb_tick_parser` injects bytes directly into the parser — perfect for exercising FSM logic fast. This bench instead wiggles the actual `uart_txd_in` wire with real start/data/stop bit timing at 115 200 baud and lets the *hardware* recover the bytes. That difference in stimulus fidelity is what makes it an *integration* test: it exercises the synchronizer, the half-bit alignment, the bit-period arithmetic, the uart_rx→parser handshake, and the timestamp latch — none of which byte injection touches. The cost is simulation time (each byte is 8 680 clock cycles), which is exactly why *both* benches should exist: fast unit tests for logic iteration, slow integration tests for interface truth. That's the same unit/integration pyramid as in software, expressed in nanoseconds.

### 8.2 Testbench constructs that don't exist in RTL

The bench is SystemVerilog but *not* synthesizable, and it uses the simulation-only half of the language deliberately: delays (`#(BIT_NS)`) to model real time, `real` arithmetic for the 8 680.6 ns bit period, `task automatic` for reusable stimulus procedures, and hierarchical references (`dut.u_timestamp.us_now`) to observe internal signals without routing them to ports. Hierarchical references deserve a caution flag: they're invaluable for *observation* in a bench and poison in RTL (they bypass the module interface contract). Here they let the bench check `arrival_us` and `parse_error_count` — signals a black-box test couldn't see — making this technically a white-box test, a reasonable choice when you own both the design and the bench.

`uart_send_byte` is the mirror image of `uart_rx`: drive low one bit period (start), drive `b[i]` for i = 0…7 (LSB first — matching §3.4), drive high one period (stop). `send_frame` then serializes the fields most-significant-byte-first via the descending loop `for (int i = 3; i >= 0; i--) ... fsym[8*i +: 8]` — the `+:` indexed part-select slices byte i, and the descending order is the big-endian wire format from MANUAL.md §2 made executable. If the format and the parser ever disagree, this bench is the arbiter.

### 8.3 Self-checking and the monitor pattern

Every check goes through `check64`/`check_near_us`, which compare, count, and print PASS/FAIL — the bench renders a verdict (`31 PASS / 0 FAIL`) rather than a waveform to eyeball. `check64` uses `===` (4-state equality) so an X or Z in a result *fails* instead of sneaking through `==`'s optimistic matching.

The pulse monitors run as free-running processes parallel to the stimulus:

```systemverilog
always @(posedge clk100) begin
    if (dut.msg_valid)   msg_valid_seen++;
    if (dut.parse_error) parse_error_seen++;
end
```

A single-cycle pulse is easy to miss if you check for it *after* sending stimulus — it fired 40 cycles ago and is gone. The monitor pattern watches every clock edge and accumulates, so the main sequence can assert on the *count* at leisure. It also catches pulses that shouldn't exist (a double-fire would make the count wrong), which point checks can't. The same idea captures the timestamp reference: an `always` block latches `us_now` at the precise edge `sof_seen` fires, giving the check an exact expected value rather than a hand-computed estimate.

The `#50ms` timeout process is boilerplate worth keeping in every bench: a stimulus bug that hangs the sequence (waiting on a pulse that never comes) otherwise runs forever in a CI pipeline.

### 8.4 The two bugs the bench caught in *itself* — and why they're worth studying

The first run scored 24/31, and both failures were testbench bugs. This is normal and educational.

**Bug 1 — the 82 vs 87 µs discrepancy.** The bench originally captured its reference timestamp after `uart_send_byte(SOF)` returned, i.e., after the stop bit's full duration: 10 bit-times ≈ 86.8 µs from the start edge. But `uart_rx` finishes a byte at the *middle* of the stop bit — 0.5 + 8 + 1 = 9.5 bit-times ≈ 82.5 µs (§3.4) — and that's when `sof_seen` fires and the latch happens. The observed error, ~5 µs, is exactly the half-bit difference. Two lessons: the bench initially encoded an *assumption* about DUT timing instead of *observing* it (the fix — latch the reference when `dut.sof_seen` actually pulses — observes); and when a test fails, quantify the error before touching anything, because "wrong by exactly half a bit period" is a diagnosis, while "wrong by roughly 5" is a guess.

**Bug 2 — the resync test that didn't reach the error.** Test 6 sends a stray 0xAA inside garbage, expecting the parser to assemble a doomed frame, hit S_EOF, error out, and resync. The garbage tail was originally 18 filler bytes — but a frame body is *21* bytes after SOF (1+4+4+2+1+8+1). With only 20 total, the spurious frame was still mid-assembly when the real test frame began, so the real frame's SOF byte got consumed *as the spurious frame's EOF* (firing parse_error — which is why the error count was correct even as the decode failed!), and the rest of the real frame fell into IDLE as noise. The fix is one more filler byte. This is precisely the failure mode the Python model discovered (MANUAL.md §3, "Insight from Test 6") — recreated independently by an off-by-one, which is a satisfying confirmation the model captured real behavior: **once a spurious SOF starts a frame, the next 21 bytes belong to it, period.** The host-bridge rule follows: after any error, don't attempt mid-frame cleverness; send the next complete frame.

---

## 9. Consolidated Design-Decision Index

For review or interview prep, every "why" in one place:

| Decision | Reasoning | Section |
|---|---|---|
| Single clock domain | CDC-free ⇒ STA can prove correctness | §1 |
| Single-cycle pulse handshakes | No ack wires; composable; rate mismatch makes backpressure unnecessary | §1 |
| Synchronous active-low reset | Matches 7-series fabric; matches board polarity | §2.2 |
| Default-assignment pulses | Exactly-one-cycle strobes with no clearing logic | §2.3 |
| `$clog2` sizing | Parameters change ⇒ counters resize automatically | §2.6 |
| Register initial values | Real hardware behavior on FPGA; kills X-propagation | §2.7 |
| 2-FF synchronizers | Metastability containment at every async input | §3.3 |
| Half-bit start delay | Glitch rejection + center-of-bit sampling alignment | §3.4 |
| Right-shift byte assembly | LSB-first UART convention | §3.4 |
| No parity/FIFO/oversampling in uart_rx | Complexity not earned by a USB-cable channel | §3.5 |
| Left-shift field assembly | Big-endian wire format; matches `struct.pack('>')` | §4.2 |
| Working vs. output registers | Transactional outputs; corruption can't reach data path | §4.3 |
| Error recovery via wrong-EOF | Bounded disturbance beats heavier framing machinery | §4.4 |
| `sof_seen` from the parser | Detection lives with context; 0xAA is legal data mid-frame | §4.5 |
| 64-bit µs counter | No rollover code ever; honest resolution | §5.1 |
| Pending/commit timestamp | Timestamp obeys the same transactional contract as data | §5.2 |
| Reset hold-off counter | Debounce + simultaneous synchronous reset release | §6.1 |
| Named port connections | Reorder-proof; review-standard | §6.2 |
| Boundary re-registration + `mark_debug` | Timing discipline + ILA-ready bring-up visibility | §6.3 |
| Saturating error counter | FFFF means "many," never lies via wraparound | §6.3 |
| Pulse stretchers + heartbeat | Human-visible diagnostics; layered debug ladder | §6.4 |
| Clock constraint | Gives STA requirements to prove | §7.2 |
| False paths only over synchronizers | XDC claim backed by RTL structure | §7.3 |
| Bit-level + byte-level benches | Unit/integration pyramid in hardware | §8.1 |
| Monitor processes for pulses | Single-cycle events can't be polled after the fact | §8.3 |
| `===` in checks | X/Z fail loudly instead of passing optimistically | §8.3 |

---

## 10. Suggested Exercises

If you want to turn reading into understanding, each of these is a contained change with a testable outcome. Modify `BAUD` to 921 600 in the testbench only, watch framing errors appear, and explain the failure using §3.2's budget. Add a CRC-8 byte to the wire format (host and parser both) and measure what it costs in states and LUTs. Make `parse_error_count` readable over UART by building the TX direction — which is the on-ramp to the result-frame layer anyway. And when the indicator engine starts, its input interface is already sitting in `top_arty.sv` as a comment, waiting.
