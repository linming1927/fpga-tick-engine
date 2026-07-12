//-----------------------------------------------------------------------------
// timestamp_us.sv — free-running 64-bit microsecond counter + arrival latch
//
// Checklist item 6 (MANUAL.md §4):
//   "Add a free-running 64-bit microsecond counter on the FPGA and latch it
//    on rx_valid at the first byte of each frame (the SOF byte)."
//
// Two-stage pending/commit scheme, so arrival_us has the same semantics as
// the tick_parser outputs (stable between msg_valid pulses):
//
//   * sof_seen  -> pending  <= us_now      (candidate arrival time)
//   * msg_valid -> arrival_us <= pending   (frame proved good — commit)
//
// If the frame turns out bad (parse_error), pending is simply overwritten by
// the next SOF; arrival_us never sees it. Downstream logic can therefore
// sample {arrival_us, host_tstamp} together on msg_valid, and
// (arrival_us - host_tstamp) is the host-to-FPGA transit latency, modulo
// the offset between the two clocks (see note below).
//
// NOTE on epoch: us_now counts microseconds since FPGA reset, NOT Unix time.
// The host bridge should compute latency as deltas between frames, or
// perform a one-time offset calibration at startup.
//
// 64-bit range at 1 MHz: ~584,000 years before rollover. No wrap handling
// is required.
//-----------------------------------------------------------------------------
`timescale 1ns / 1ps
`default_nettype none

module timestamp_us #(
    parameter int CLK_HZ = 100_000_000
)(
    input  wire  logic        clk,
    input  wire  logic        rst_n,       // active-low synchronous reset

    input  wire  logic        sof_seen,    // from tick_parser: SOF byte accepted
    input  wire  logic        msg_valid,   // from tick_parser: frame good

    output logic [63:0] us_now,      // free-running microseconds since reset
    output logic [63:0] pending_us,  // arrival time of the frame IN FLIGHT —
                                     // latched at SOF, stable thereafter.
                                     // Consumers that sample ON the msg_valid
                                     // cycle must use THIS one: arrival_us
                                     // below commits on the same edge those
                                     // consumers sample, so they'd read the
                                     // previous frame's value (a one-cycle
                                     // race found by tb_tx_integration T1).
    output logic [63:0] arrival_us   // FPGA arrival time of last GOOD frame;
                                     // stable BETWEEN msg_valid pulses —
                                     // for ILA / always-readable consumers
);

    localparam int CLKS_PER_US = CLK_HZ / 1_000_000;  // 100 @ 100 MHz
    localparam int DIV_W       = $clog2(CLKS_PER_US);

    logic [DIV_W-1:0] div_cnt;
    logic [63:0]      pending;

    assign pending_us = pending;

    always_ff @(posedge clk) begin
        if (!rst_n) begin
            div_cnt    <= '0;
            us_now     <= '0;
            pending    <= '0;
            arrival_us <= '0;
        end else begin
            // ---- free-running microsecond tick -----------------------------
            if (div_cnt == CLKS_PER_US - 1) begin
                div_cnt <= '0;
                us_now  <= us_now + 64'd1;
            end else begin
                div_cnt <= div_cnt + 1'b1;
            end

            // ---- latch on SOF, commit on good EOF --------------------------
            if (sof_seen)
                pending <= us_now;

            if (msg_valid)
                arrival_us <= pending;
        end
    end

endmodule

`default_nettype wire
