//-----------------------------------------------------------------------------
// ema_engine.sv — EMA crossover detector (the SMA engine's sibling)
//
// Exponential moving average with power-of-two smoothing:
//     ema' = ema + (price - ema) * 2^-K
// Where the SMA engine keeps an N-deep window buffer and a running sum, the
// EMA keeps NO history at all — one accumulator per average. Recent prices
// weigh exponentially more, so the EMA reacts faster to moves than an SMA
// of comparable "length" (rule of thumb: alpha = 2/(N+1), so K=3 ~ N=15,
// K=5 ~ N=63).
//
// ---- Extended-precision accumulator (the important trick) -------------------
// Storing the EMA itself as an integer and updating with a >>K creates a
// truncation DEADBAND: once |price - ema| < 2^K the shift yields 0 and the
// EMA freezes short of the price. Instead each average stores the SCALED
// accumulator A = ema << K and updates as a leaky integrator:
//     A' = A + price - (A >> K)          // all integer, no multiply
//     ema = A >> K                        // read-out only
// The K fractional bits retained inside A shrink the deadband to the
// read-out quantization (1 price LSB = $0.0001). One adder, one subtractor,
// one wire-shift per average — cheaper than the SMA's window RAM.
//
// A's width: the fixpoint is price_max << K, and the update overshoot is
// bounded by one price, so 32 + K + 1 bits never overflows.
//
// ---- Seeding, warm-up, priming ------------------------------------------------
// First accepted trade seeds both accumulators (A = price << K) — the
// standard convention. The EMA is *defined* from tick 1 but not *trusted*:
// signals stay gated until WARMUP_N accepted trades (parameter; default
// matches the SMA engine's SLOW_N so the two strategies come online
// together and the comparison is fair). The first post-warm-up evaluation
// primes above_prev without firing — same rule, same reason, as the SMA
// engine (see indicator_engine.sv header).
//
// Pipeline mirrors the SMA engine: S1 accumulate, S2 register the read-outs,
// S3 compare and fire. Same strict comparison (fast > slow), so host mirror
// models share one crossover convention across both strategies.
//-----------------------------------------------------------------------------
`timescale 1ns / 1ps
`default_nettype none

module ema_engine #(
        parameter int          K_FAST   = 3,     // alpha = 1/8
    parameter int          K_SLOW   = 5,     // alpha = 1/32  (> K_FAST)
    parameter int          WARMUP_N = 32     // trades before signals allowed
)(
    input  wire  logic        clk,
    input  wire  logic        rst_n,

    // runtime symbol slot (from the symcfg register file, v2.0):
    input  wire  logic [47:0] target_symbol,   // 6 ASCII bytes, space padded
    input  wire  logic        slot_en,         // slot is configured/valid
    input  wire  logic        state_rst,       // 1-cycle: this slot was
                                               // (re)written — start fresh.
                                               // Writing a slot ALWAYS
                                               // resets its engine state,
                                               // so host mirror models can
                                               // rebuild in lockstep.

    input  wire  logic        tick_valid,
    input  wire  logic [7:0]  msg_type,
    input  wire  logic [47:0] symbol,
    input  wire  logic [31:0] price,
    input  wire  logic [63:0] host_ts,
    input  wire  logic [63:0] fpga_ts,

    output logic        signal_valid,     // 1-cycle pulse
    output logic [47:0] signal_symbol,    // which symbol crossed
    output logic [7:0]  signal_side,      // 0x01 buy / 0x02 sell
    output logic [31:0] signal_price,
    output logic [63:0] signal_host_ts,
    output logic [63:0] signal_fpga_ts,

    output logic [31:0] ema_fast,         // read-outs, registered per tick
    output logic [31:0] ema_slow,
    output logic        emas_valid        // warm-up complete
);

    localparam int AF_W = 32 + K_FAST + 1;
    localparam int AS_W = 32 + K_SLOW + 1;

    localparam logic [7:0] TYPE_TRADE = 8'h01;
    localparam logic [7:0] SIDE_BUY   = 8'h01;
    localparam logic [7:0] SIDE_SELL  = 8'h02;

    logic accept;
    assign accept = tick_valid
                 && slot_en
                 && (symbol   == target_symbol)
                 && (msg_type == TYPE_TRADE);

    //-------------------------------------------------------------------------
    // Stage 1 — leaky-integrator accumulators
    //-------------------------------------------------------------------------
    logic [AF_W-1:0] acc_fast;
    logic [AS_W-1:0] acc_slow;
    logic            seeded;
    logic [$clog2(WARMUP_N+1)-1:0] warm_cnt;

    logic [31:0] meta_price;
    logic [47:0] meta_symbol;
    logic [63:0] meta_host_ts, meta_fpga_ts;
    logic p1, p2;

    always_ff @(posedge clk) begin
        if (!rst_n) begin
            acc_fast     <= '0;
            acc_slow     <= '0;
            seeded       <= 1'b0;
            warm_cnt     <= '0;
            meta_price   <= '0;
            meta_symbol  <= '0;
            meta_host_ts <= '0;
            meta_fpga_ts <= '0;
            p1           <= 1'b0;
        end else if (state_rst) begin
            // slot rewritten: clear all window state (same as reset,
            // minus the clock-domain plumbing)
            acc_fast <= '0;
            acc_slow <= '0;
            seeded   <= 1'b0;
            warm_cnt <= '0;
            p1       <= 1'b0;
        end else begin
            p1 <= 1'b0;
            if (accept) begin
                if (!seeded) begin
                    acc_fast <= AF_W'(price) << K_FAST;   // seed = first price
                    acc_slow <= AS_W'(price) << K_SLOW;
                    seeded   <= 1'b1;
                end else begin
                    acc_fast <= acc_fast + AF_W'(price)
                                         - (acc_fast >> K_FAST);
                    acc_slow <= acc_slow + AS_W'(price)
                                         - (acc_slow >> K_SLOW);
                end
                if (warm_cnt != WARMUP_N[$clog2(WARMUP_N+1)-1:0])
                    warm_cnt <= warm_cnt + 1'b1;
                meta_price   <= price;
                meta_symbol  <= symbol;
                meta_host_ts <= host_ts;
                meta_fpga_ts <= fpga_ts;
                p1           <= 1'b1;
            end
        end
    end

    assign emas_valid = (warm_cnt == WARMUP_N[$clog2(WARMUP_N+1)-1:0]);

    //-------------------------------------------------------------------------
    // Stage 2 — register the read-outs (ema = A >> K)
    //-------------------------------------------------------------------------
    always_ff @(posedge clk) begin
        if (!rst_n) begin
            ema_fast <= '0;
            ema_slow <= '0;
            p2       <= 1'b0;
        end else begin
            p2 <= p1;
            if (p1) begin
                ema_fast <= 32'(acc_fast >> K_FAST);
                ema_slow <= 32'(acc_slow >> K_SLOW);
            end
        end
    end

    //-------------------------------------------------------------------------
    // Stage 3 — crossover decision (identical convention to the SMA engine)
    //-------------------------------------------------------------------------
    logic above_prev, primed, above_now;
    assign above_now = (ema_fast > ema_slow);

    always_ff @(posedge clk) begin
        if (!rst_n) begin
            above_prev     <= 1'b0;
            primed         <= 1'b0;
            signal_valid   <= 1'b0;
            signal_symbol  <= '0;
            signal_side    <= '0;
            signal_price   <= '0;
            signal_host_ts <= '0;
            signal_fpga_ts <= '0;
        end else if (state_rst) begin
            above_prev   <= 1'b0;
            primed       <= 1'b0;
            signal_valid <= 1'b0;
        end else begin
            signal_valid <= 1'b0;
            if (p2 && emas_valid) begin
                if (primed && (above_now != above_prev)) begin
                    signal_valid   <= 1'b1;
                    signal_symbol  <= meta_symbol;
                    signal_side    <= above_now ? SIDE_BUY : SIDE_SELL;
                    signal_price   <= meta_price;
                    signal_host_ts <= meta_host_ts;
                    signal_fpga_ts <= meta_fpga_ts;
                end
                above_prev <= above_now;
                primed     <= 1'b1;
            end
        end
    end

endmodule

`default_nettype wire
