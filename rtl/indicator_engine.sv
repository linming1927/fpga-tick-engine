//-----------------------------------------------------------------------------
// indicator_engine.sv — SMA crossover detector (golden cross / death cross)
//
// Watches the decoded tick stream for TRADES of one target symbol, maintains
// two simple moving averages (fast and slow), and fires a one-cycle signal
// pulse when the fast SMA crosses the slow SMA:
//     fast crosses ABOVE slow  -> BUY  (golden cross), signal_side = 0x01
//     fast crosses BELOW slow  -> SELL (death cross),  signal_side = 0x02
//
// ---- The running-sum trick --------------------------------------------------
// A naive N-sample average re-adds N values per tick. Instead each window
// keeps a running sum and a shift-register of the last N prices:
//
//     sum <= sum + newest - oldest;
//
// One add and one subtract per tick regardless of N. The buffers power up as
// zeros, so during warm-up the "oldest" being subtracted is 0 and the sum is
// simply the sum of what's arrived — correct with no special-case logic.
//
// N is required to be a power of two so that the average is a plain wire:
//     sma = sum >> $clog2(N)          (truncating integer divide)
// Prices are fixed-point (x10 000), so the LSB lost to truncation is
// $0.0001 — irrelevant against crossover decisions.
//
// ---- Warm-up and priming ----------------------------------------------------
// SMAs are meaningless until their windows are full. smas_valid rises when
// the SLOW window (the larger) has SLOW_N samples. The first comparison after
// warm-up only PRIMES the above/below state — it cannot fire a signal,
// because "crossing" requires a previous state to have crossed FROM. Without
// priming, the engine would emit a spurious signal on its first valid sample.
//
// ---- Pipeline ----------------------------------------------------------------
// Three registered stages, one tick in flight at a time (ticks are ~191 000
// clock cycles apart at 115 200 baud — the pipeline is for timing clarity,
// not throughput):
//   S1 (accept):  update buffers + running sums, latch tick metadata
//   S2 (average): register sma_fast / sma_slow  (shift of the new sums)
//   S3 (decide):  compare, detect edge vs above_prev, fire signal_valid
// Signal latency: 3 clocks = 30 ns after tick_valid. Nothing downstream can
// tell the difference; the win is that each stage is trivially readable and
// no path chains adder -> shifter -> comparator in one cycle.
//
// ---- Ordering subtlety (equal SMAs) ------------------------------------------
// "above" is defined as (fast > slow), strictly. If the SMAs are exactly
// equal, above = 0. A move from above to exactly-equal therefore reads as a
// death cross; equal-then-higher reads as a golden cross. Any convention
// here is defensible — this one is documented so host models can match it.
//-----------------------------------------------------------------------------
`timescale 1ns / 1ps
`default_nettype none

module indicator_engine #(
    parameter logic [31:0] TARGET_SYMBOL = "SPY ",  // 4 ASCII bytes, packed
    parameter int          FAST_N        = 8,       // power of two
    parameter int          SLOW_N        = 32       // power of two, > FAST_N
)(
    input  wire  logic        clk,
    input  wire  logic        rst_n,

    // decoded tick bus (from top_arty's tick_* registers)
    input  wire  logic        tick_valid,
    input  wire  logic [7:0]  msg_type,
    input  wire  logic [31:0] symbol,
    input  wire  logic [31:0] price,
    input  wire  logic [63:0] host_ts,
    input  wire  logic [63:0] fpga_ts,

    // signal out (all fields valid while signal_valid pulses)
    output logic        signal_valid,     // 1-cycle pulse
    output logic [7:0]  signal_side,      // 0x01 buy / 0x02 sell
    output logic [31:0] signal_price,     // trade price that triggered it
    output logic [63:0] signal_host_ts,   // host timestamp of that trade
    output logic [63:0] signal_fpga_ts,   // FPGA arrival of that trade

    // status / debug (registered every accepted tick, after warm-up)
    output logic [31:0] sma_fast,
    output logic [31:0] sma_slow,
    output logic        smas_valid        // slow window is full
);

    localparam int LOG2_FAST = $clog2(FAST_N);
    localparam int LOG2_SLOW = $clog2(SLOW_N);
    localparam int FSUM_W    = 32 + LOG2_FAST;   // sum of FAST_N 32-bit values
    localparam int SSUM_W    = 32 + LOG2_SLOW;

    localparam logic [7:0] TYPE_TRADE = 8'h01;
    localparam logic [7:0] SIDE_BUY   = 8'h01;
    localparam logic [7:0] SIDE_SELL  = 8'h02;

    // this tick is ours: right symbol, and a trade (quotes carry two-sided
    // prices with different semantics — mixing them into a trade SMA would
    // corrupt it; quote handling is a future indicator's problem)
    logic accept;
    assign accept = tick_valid
                 && (symbol   == TARGET_SYMBOL)
                 && (msg_type == TYPE_TRADE);

    //-------------------------------------------------------------------------
    // Stage 1 — window update
    //-------------------------------------------------------------------------
    logic [31:0]       fast_buf [0:FAST_N-1];
    logic [31:0]       slow_buf [0:SLOW_N-1];
    logic [FSUM_W-1:0] sum_fast;
    logic [SSUM_W-1:0] sum_slow;
    logic [LOG2_SLOW:0] fill_cnt;          // counts up to SLOW_N then holds

    // tick metadata rides alongside the pipeline so a signal in S3 reports
    // the exact trade that caused it
    logic [31:0] meta_price;
    logic [63:0] meta_host_ts, meta_fpga_ts;

    logic p1, p2;                          // pipeline follow pulses

    always_ff @(posedge clk) begin
        if (!rst_n) begin
            for (int i = 0; i < FAST_N; i++) fast_buf[i] <= '0;
            for (int i = 0; i < SLOW_N; i++) slow_buf[i] <= '0;
            sum_fast     <= '0;
            sum_slow     <= '0;
            fill_cnt     <= '0;
            meta_price   <= '0;
            meta_host_ts <= '0;
            meta_fpga_ts <= '0;
            p1           <= 1'b0;
        end else begin
            p1 <= 1'b0;
            if (accept) begin
                // shift registers: newest at [0], oldest falls off [N-1].
                // Non-blocking semantics make this read-then-shift safe:
                // every right-hand side samples PRE-edge values.
                for (int i = FAST_N-1; i > 0; i--) fast_buf[i] <= fast_buf[i-1];
                for (int i = SLOW_N-1; i > 0; i--) slow_buf[i] <= slow_buf[i-1];
                fast_buf[0] <= price;
                slow_buf[0] <= price;

                sum_fast <= sum_fast + price - fast_buf[FAST_N-1];
                sum_slow <= sum_slow + price - slow_buf[SLOW_N-1];

                if (fill_cnt != SLOW_N[LOG2_SLOW:0])
                    fill_cnt <= fill_cnt + 1'b1;

                meta_price   <= price;
                meta_host_ts <= host_ts;
                meta_fpga_ts <= fpga_ts;
                p1           <= 1'b1;
            end
        end
    end

    assign smas_valid = (fill_cnt == SLOW_N[LOG2_SLOW:0]);

    //-------------------------------------------------------------------------
    // Stage 2 — averages (the divide-by-shift)
    //-------------------------------------------------------------------------
    always_ff @(posedge clk) begin
        if (!rst_n) begin
            sma_fast <= '0;
            sma_slow <= '0;
            p2       <= 1'b0;
        end else begin
            p2 <= p1;
            if (p1) begin
                sma_fast <= 32'(sum_fast >> LOG2_FAST);
                sma_slow <= 32'(sum_slow >> LOG2_SLOW);
            end
        end
    end

    //-------------------------------------------------------------------------
    // Stage 3 — crossover decision
    //-------------------------------------------------------------------------
    logic above_prev;
    logic primed;          // above_prev holds a real value (first-eval guard)
    logic above_now;

    assign above_now = (sma_fast > sma_slow);

    always_ff @(posedge clk) begin
        if (!rst_n) begin
            above_prev     <= 1'b0;
            primed         <= 1'b0;
            signal_valid   <= 1'b0;
            signal_side    <= '0;
            signal_price   <= '0;
            signal_host_ts <= '0;
            signal_fpga_ts <= '0;
        end else begin
            signal_valid <= 1'b0;
            if (p2 && smas_valid) begin
                if (primed && (above_now != above_prev)) begin
                    signal_valid   <= 1'b1;
                    signal_side    <= above_now ? SIDE_BUY : SIDE_SELL;
                    signal_price   <= meta_price;
                    signal_host_ts <= meta_host_ts;
                    signal_fpga_ts <= meta_fpga_ts;
                end
                above_prev <= above_now;
                primed     <= 1'b1;      // first pass primes, never fires
            end
        end
    end

endmodule

`default_nettype wire
