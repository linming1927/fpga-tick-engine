//-----------------------------------------------------------------------------
// sync_fifo.sv — synchronous first-word-fall-through (FWFT) FIFO
//
// Decouples the tick decode rate from the (slower) echo transmit rate.
// The math that makes it necessary: an inbound frame is 22 bytes (~1.91 ms
// at 115 200 baud) but the echo frame is 30 bytes (~2.60 ms). Under
// back-to-back input the TX side falls behind by ~0.69 ms per frame, so a
// buffer absorbs bursts and, when it fills, the WRITER drops (counted in
// top_arty) rather than corrupting anything. Real tick rates are far below
// back-to-back, so depth 16 is generous in practice — but the drop path
// exists because "can't happen" traffic assumptions are how systems break.
//
// FWFT means rd_data always shows the oldest entry combinationally whenever
// !empty — no "read latency" cycle. The consumer looks at rd_data, and
// pulses rd_en to advance past it. This makes frame_tx's FSM one state
// simpler than with a registered-read FIFO.
//
// Pointer scheme (the classic one): pointers carry log2(DEPTH)+1 bits — one
// extra "wrap" MSB beyond what's needed to index memory.
//     empty : pointers identical, wrap bits included
//     full  : address bits identical, wrap bits DIFFER
// i.e. full means the writer has lapped the reader exactly once. This
// distinguishes full from empty (both have equal address bits) without a
// separate element counter. DEPTH must be a power of two for the wrap
// arithmetic to work.
//-----------------------------------------------------------------------------
`timescale 1ns / 1ps
`default_nettype none

module sync_fifo #(
    parameter int WIDTH = 224,
    parameter int DEPTH = 16          // must be a power of two
)(
    input  wire  logic             clk,
    input  wire  logic             rst_n,

    // write side
    input  wire  logic             wr_en,     // write accepted iff !full
    input  wire  logic [WIDTH-1:0] wr_data,
    output logic             full,

    // read side (FWFT)
    output logic [WIDTH-1:0] rd_data,   // oldest entry, valid iff !empty
    input  wire  logic             rd_en,     // advance past rd_data
    output logic             empty
);

    localparam int AW = $clog2(DEPTH);

    logic [WIDTH-1:0] mem [0:DEPTH-1];
    logic [AW:0]      wr_ptr, rd_ptr;         // AW+1 bits: address + wrap bit

    assign empty = (wr_ptr == rd_ptr);
    assign full  = (wr_ptr[AW] != rd_ptr[AW]) &&
                   (wr_ptr[AW-1:0] == rd_ptr[AW-1:0]);

    assign rd_data = mem[rd_ptr[AW-1:0]];     // FWFT: combinational peek

    always_ff @(posedge clk) begin
        if (!rst_n) begin
            wr_ptr <= '0;
            rd_ptr <= '0;
        end else begin
            if (wr_en && !full) begin
                mem[wr_ptr[AW-1:0]] <= wr_data;
                wr_ptr              <= wr_ptr + 1'b1;
            end
            if (rd_en && !empty) begin
                rd_ptr <= rd_ptr + 1'b1;
            end
        end
    end

    // mem itself is not reset — standard practice: resetting a RAM costs a
    // cycle per word or forces register implementation; the pointers being
    // reset already guarantees no stale word is ever *visible*.

endmodule

`default_nettype wire
