//-----------------------------------------------------------------------------
// symcfg.sv — runtime symbol register file, written over UART
//
// Eight slots of {valid, 48-bit symbol}. Each slot feeds one SMA + one EMA
// engine instance, so the traded symbol set is now a RUNTIME configuration
// — no bitstream rebuild to change symbols.
//
// The write path deliberately reuses the existing tick frame (TYPE 0x10):
//   SYMBOL field = the 6-char symbol to store (space padded)
//   QTY[2:0]     = slot index 0..7
//   SIDE         = 0x01 set/enable, 0x00 clear/disable
// No new frame format, no second parser FSM — the tick_parser already
// delivers every field this module needs, and the echo path already
// returns every decoded frame to the host, so the 0x90 echo of a config
// frame IS the acknowledgement: the host reads back the slot index and the
// symbol the fabric actually stored. Configuration is thereby verified
// end-to-end through the same path as data, with zero added machinery.
//
// Reset state: slot 0 = DEFAULT_SYM0 ("SPY   "), valid; slots 1-7 empty.
// This preserves the pre-v2 out-of-box behavior — the board trades SPY
// until told otherwise.
//
// Duplicate symbols across slots are NOT rejected here (the host enforces
// uniqueness); if two slots hold the same symbol, the lower slot's engines
// win signal arbitration in top_arty and the other slot's same-cycle
// signal is lost — documented, not defended.
//-----------------------------------------------------------------------------
`timescale 1ns / 1ps
`default_nettype none

module symcfg #(
    parameter logic [47:0] DEFAULT_SYM0 = "SPY   "
)(
    input  wire  logic        clk,
    input  wire  logic        rst_n,

    // decoded frame bus (top_arty's registered tick_* signals)
    input  wire  logic        tick_valid,
    input  wire  logic [7:0]  msg_type,
    input  wire  logic [47:0] symbol,
    input  wire  logic [15:0] qty,       // [2:0] = slot index
    input  wire  logic [7:0]  side,      // [0] = set(1)/clear(0)

    output logic [7:0]   slot_valid,
    output logic [7:0]   slot_wr,      // 1-cycle pulse: slot g was written
                                       // (set OR clear) — resets its engines
    // Slots as ONE flat packed vector, slot g at [g*48 +: 48].
    // Deliberately not an unpacked array port: Icarus Verilog does not
    // propagate unpacked-array ports across module boundaries (found the
    // hard way — inside reads fine, outside reads X), and the benches must
    // run under both iverilog and Vivado. A packed vector is portable.
    output logic [383:0] slots_flat
);

    localparam logic [7:0] TYPE_SYMCFG = 8'h10;

    always_ff @(posedge clk) begin
        if (!rst_n) begin
            slots_flat <= { 336'd0, DEFAULT_SYM0 };   // slot 0 seeded
            slot_valid <= 8'h01;
            slot_wr    <= 8'h00;
        end else begin
            slot_wr <= 8'h00;
            if (tick_valid && msg_type == TYPE_SYMCFG) begin
                slot_wr[qty[2:0]] <= 1'b1;
                if (side[0]) begin
                    slots_flat[qty[2:0]*48 +: 48] <= symbol;
                    slot_valid[qty[2:0]]          <= 1'b1;
                end else begin
                    slot_valid[qty[2:0]] <= 1'b0;
                end
            end
        end
    end

endmodule

`default_nettype wire
