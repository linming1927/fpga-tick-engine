//-----------------------------------------------------------------------------
// frame_tx.sv — echo/result frame serializer with two-level priority
//
// Pops 224-bit records from TWO FWFT FIFOs and streams each as a 30-byte
// FPGA→host frame. The HI port (indicator signals) always wins arbitration
// over the LO port (tick echoes): when both FIFOs hold data, the next frame
// sent is a signal.
//
// Why priority matters here: the echo path runs near saturation by design
// (30-byte echoes vs 22-byte input frames), so echo drops are an accepted,
// counted behavior. A BUY/SELL signal must never share that fate — it gets
// its own small FIFO (which practically never fills: signals are rare
// events) and first claim on the transmitter. This is quality-of-service
// queuing in miniature: classify traffic, queue separately, serve by
// priority. Arbitration is per-frame, never mid-frame — a frame in flight
// always completes; a signal arriving mid-echo waits at most one frame time
// (~2.6 ms), preserving frame atomicity on the wire.
//
// FPGA → host wire format (30 bytes, all multi-byte fields big-endian):
//
//   Offset  Field     Size  Encoding      (wire format v2 — 32 bytes)
//   ------  --------  ----  ------------------------------------------------
//     0     SOF        1 B  0xBB
//     1     TYPE       1 B  0x80 | record type:
//                             0x81 trade echo   0x82 quote echo
//                             0x83 SMA signal   0x84 EMA signal
//                             0x90 symbol-config ack (echo of TYPE 0x10;
//                                  SYMBOL = slot contents, QTY[2:0] = slot,
//                                  SIDE = 1 set / 0 clear)
//    2-7    SYMBOL     6 B  (offsets below all shift +2 vs v1)
//    2-5    SYMBOL     4 B  ticker
//    6-9    PRICE      4 B  price x 10 000
//   10-11   QTY        2 B  echo: as received | signal: 0x0000
//    12     SIDE       1 B  echo: as received | signal: 0x01 buy / 0x02 sell
//   13-20   TS_A       8 B  echo: HOST_TS of the tick
//                           signal: {SMA_FAST[31:0], SMA_SLOW[31:0]} — a
//                           tagged union: the field's meaning follows TYPE.
//                           Carrying the FPGA-computed SMAs home lets the
//                           host bridge verify the hardware math against
//                           its own model on every signal.
//    21-28   FPGA_TS   8 B  FPGA arrival timestamp (us since reset) of the
//                           tick (echo) / triggering trade (signal)
//    29     EOF        1 B  0xCC
//
// Record layout on both FIFOs (packed by top_arty, 224 bits):
//   [223:216] type   [215:184] symbol  [183:152] price
//   [151:136] qty    [135:128] side    [127:64] ts_a    [63:0] fpga_ts
//
// Serialization: the frame is assembled once into a 240-bit vector at pop
// time; each byte is then frame_vec[(239 - 8*byte_idx) -: 8].
//-----------------------------------------------------------------------------
`timescale 1ns / 1ps
`default_nettype none

module frame_tx (
    input  wire  logic         clk,
    input  wire  logic         rst_n,

    // HI-priority pop port (indicator signals) — FWFT
    input  wire  logic         hi_empty,
    input  wire  logic [239:0] hi_rd_data,
    output logic         hi_rd_en,

    // LO-priority pop port (tick echoes) — FWFT
    input  wire  logic         lo_empty,
    input  wire  logic [239:0] lo_rd_data,
    output logic         lo_rd_en,

    // uart_tx side (valid/ready)
    output logic [7:0]   tx_data,
    output logic         tx_valid,
    input  wire  logic         tx_ready
);

    localparam int FRAME_BYTES = 32;
    localparam logic [7:0] SOF_BYTE = 8'hBB;
    localparam logic [7:0] EOF_BYTE = 8'hCC;

    typedef enum logic { F_IDLE, F_SEND } state_t;
    state_t state = F_IDLE;

    logic [255:0] frame_vec;
    logic [4:0]   byte_idx;

    assign tx_data  = frame_vec[(255 - 8*byte_idx) -: 8];
    assign tx_valid = (state == F_SEND);

    always_ff @(posedge clk) begin
        if (!rst_n) begin
            state     <= F_IDLE;
            frame_vec <= '0;
            byte_idx  <= '0;
            hi_rd_en  <= 1'b0;
            lo_rd_en  <= 1'b0;
        end else begin
            hi_rd_en <= 1'b0;
            lo_rd_en <= 1'b0;

            unique case (state)
                //-------------------------------------------------------------
                // Fixed-priority arbitration, decided fresh for every frame:
                // signals first, echoes only when no signal is waiting.
                F_IDLE: begin
                    byte_idx <= '0;
                    if (!hi_empty) begin
                        frame_vec <= { SOF_BYTE,
                                       (8'h80 | hi_rd_data[239:232]),
                                       hi_rd_data[231:0],
                                       EOF_BYTE };
                        hi_rd_en <= 1'b1;
                        state    <= F_SEND;
                    end else if (!lo_empty) begin
                        frame_vec <= { SOF_BYTE,
                                       (8'h80 | lo_rd_data[239:232]),
                                       lo_rd_data[231:0],
                                       EOF_BYTE };
                        lo_rd_en <= 1'b1;
                        state    <= F_SEND;
                    end
                end
                //-------------------------------------------------------------
                F_SEND: begin
                    if (tx_ready) begin
                        if (byte_idx == FRAME_BYTES - 1)
                            state <= F_IDLE;
                        else
                            byte_idx <= byte_idx + 1'b1;
                    end
                end
            endcase
        end
    end

endmodule

`default_nettype wire
