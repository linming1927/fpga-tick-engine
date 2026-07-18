//-----------------------------------------------------------------------------
// sessctl.sv — host-commanded session reset, written over UART
//
// The VWAP engine needs one concept nothing else in the fabric has: a
// TRADING-SESSION BOUNDARY (its accumulators are only meaningful within
// one session — see vwap_engine.sv header). The host knows the market
// calendar (ET days, DST, holidays, half-days); the fabric deliberately
// does not — building calendar logic in hardware would be new machinery
// with real bug surface, replicating something Python already does
// correctly, and it would still be wrong the first time the exchange
// changes its schedule.
//
// So: the host SENDS the boundary, one control frame at session open,
// and this module turns it into per-slot 1-cycle reset pulses. The write
// path reuses the existing tick frame exactly the way symcfg does:
//   TYPE 0x11 (TYPE_SESSRST)
//   QTY[2:0]  = slot index 0..7   (which slot's session restarts)
//   SIDE      = 0xFF -> ALL slots (the normal session-open broadcast);
//               anything else -> just the indexed slot
// No new frame format, no second parser FSM — and the echo path already
// returns every decoded frame to the host, so the 0x91 echo of a session
// frame IS the acknowledgement, verified end-to-end through the same path
// as data. Same philosophy, same zero added machinery, as symcfg.
//
// Per-slot (not global) on purpose: a mid-day slot reconfiguration to a
// new symbol already resets that slot's engines via symcfg's slot_wr; a
// single-slot sess_rst additionally lets the host restart ONE symbol's
// VWAP session (e.g. after a trading halt reopens with an auction print
// that shouldn't anchor the band) without touching the other seven.
//-----------------------------------------------------------------------------
`timescale 1ns / 1ps
`default_nettype none

module sessctl (
    input  wire  logic        clk,
    input  wire  logic        rst_n,

    // decoded frame bus (top_arty's registered tick_* signals)
    input  wire  logic        tick_valid,
    input  wire  logic [7:0]  msg_type,
    input  wire  logic [15:0] qty,       // [2:0] = slot index
    input  wire  logic [7:0]  side,      // 0xFF = broadcast all slots

    output logic [7:0]  sess_rst         // 1-cycle pulse per slot
);

    localparam logic [7:0] TYPE_SESSRST = 8'h11;

    always_ff @(posedge clk) begin
        if (!rst_n) begin
            sess_rst <= 8'h00;
        end else begin
            sess_rst <= 8'h00;
            if (tick_valid && msg_type == TYPE_SESSRST) begin
                if (side == 8'hFF)
                    sess_rst <= 8'hFF;
                else
                    sess_rst[qty[2:0]] <= 1'b1;
            end
        end
    end

endmodule

`default_nettype wire
