//-----------------------------------------------------------------------------
// tick_parser.sv — 22-byte tick frame parser FSM
//
// Wire format (MANUAL.md §2), all multi-byte fields big-endian:
//
//   Offset  Field    Size
//     0     SOF      1  (0xAA)
//     1     TYPE     1  (0x01 trade / 0x02 quote)
//    2-5    SYMBOL   4  (ASCII, MSB first, space padded)
//    6-9    PRICE    4  (uint32, price x 10000)
//   10-11   QTY      2  (uint16)
//    12     SIDE     1  (0x01 buy / 0x02 sell / 0x00 neutral)
//   13-20   TSTAMP   8  (uint64, host microseconds)
//    21     EOF      1  (0x55)
//
// Behavior:
//   * Outputs latch ONLY on a good EOF -> msg_valid pulses one cycle.
//   * Bad EOF -> parse_error pulses one cycle, outputs unchanged.
//   * msg_valid and parse_error are mutually exclusive 1-cycle pulses.
//   * Recovery to IDLE is immediate; next SOF accepted with zero dead cycles.
//
// INTEGRATION ADDITION (checklist item 6): sof_seen pulses for one cycle
// when the SOF byte of a frame is accepted in IDLE. The top level uses this
// to latch the free-running FPGA microsecond counter — the FPGA-side arrival
// timestamp, independent of host_tstamp.
//-----------------------------------------------------------------------------
`timescale 1ns / 1ps
`default_nettype none

module tick_parser (
    input  wire  logic        clk,
    input  wire  logic        rst_n,        // active-low synchronous reset

    // byte stream in (from uart_rx or any byte source)
    input  wire  logic [7:0]  rx_data,
    input  wire  logic        rx_valid,     // 1-cycle pulse: rx_data is valid

    // decoded tick, stable between msg_valid pulses
    output logic [7:0]  msg_type,     // 0x01 trade / 0x02 quote
    output logic [31:0] symbol,       // 4 ASCII bytes packed, MSB first
    output logic [31:0] price,        // price x 10000
    output logic [15:0] qty,          // share count
    output logic [7:0]  side,         // 0x01 buy / 0x02 sell / 0x00 neutral
    output logic [63:0] host_tstamp,  // host microsecond timestamp

    output logic        msg_valid,    // 1-cycle pulse: outputs just updated
    output logic        parse_error,  // 1-cycle pulse: bad EOF, outputs held
    output logic        sof_seen      // 1-cycle pulse: SOF byte accepted
);

    localparam logic [7:0] SOF_BYTE = 8'hAA;
    localparam logic [7:0] EOF_BYTE = 8'h55;

    typedef enum logic [2:0] {
        S_IDLE, S_TYPE, S_SYMBOL, S_PRICE, S_QTY, S_SIDE, S_TSTAMP, S_EOF
    } state_t;
    state_t state = S_IDLE;

    // working accumulators — assembled by left-shift (big-endian on the wire)
    logic [7:0]  r_type;
    logic [31:0] r_symbol;
    logic [31:0] r_price;
    logic [15:0] r_qty;
    logic [7:0]  r_side;
    logic [63:0] r_tstamp;

    logic [2:0]  byte_cnt;   // counts bytes within multi-byte fields (max 8)

    always_ff @(posedge clk) begin
        if (!rst_n) begin
            state       <= S_IDLE;
            byte_cnt    <= '0;
            r_type      <= '0;
            r_symbol    <= '0;
            r_price     <= '0;
            r_qty       <= '0;
            r_side      <= '0;
            r_tstamp    <= '0;
            msg_type    <= '0;
            symbol      <= '0;
            price       <= '0;
            qty         <= '0;
            side        <= '0;
            host_tstamp <= '0;
            msg_valid   <= 1'b0;
            parse_error <= 1'b0;
            sof_seen    <= 1'b0;
        end else begin
            // strobes are single-cycle pulses
            msg_valid   <= 1'b0;
            parse_error <= 1'b0;
            sof_seen    <= 1'b0;

            if (rx_valid) begin
                unique case (state)
                    //---------------------------------------------------------
                    // Hunt for the start-of-frame sentinel. Anything else is
                    // ignored (this is how the parser resyncs after garbage).
                    S_IDLE: begin
                        if (rx_data == SOF_BYTE) begin
                            state    <= S_TYPE;
                            byte_cnt <= '0;
                            sof_seen <= 1'b1;
                        end
                    end
                    //---------------------------------------------------------
                    S_TYPE: begin
                        r_type   <= rx_data;
                        state    <= S_SYMBOL;
                        byte_cnt <= '0;
                    end
                    //---------------------------------------------------------
                    // Multi-byte fields: shift left 8, OR new byte into LSB.
                    // After N bytes the register holds the big-endian value.
                    S_SYMBOL: begin
                        r_symbol <= { r_symbol[23:0], rx_data };
                        if (byte_cnt == 3'd3) begin
                            state    <= S_PRICE;
                            byte_cnt <= '0;
                        end else begin
                            byte_cnt <= byte_cnt + 1'b1;
                        end
                    end
                    //---------------------------------------------------------
                    S_PRICE: begin
                        r_price <= { r_price[23:0], rx_data };
                        if (byte_cnt == 3'd3) begin
                            state    <= S_QTY;
                            byte_cnt <= '0;
                        end else begin
                            byte_cnt <= byte_cnt + 1'b1;
                        end
                    end
                    //---------------------------------------------------------
                    S_QTY: begin
                        r_qty <= { r_qty[7:0], rx_data };
                        if (byte_cnt == 3'd1) begin
                            state    <= S_SIDE;
                            byte_cnt <= '0;
                        end else begin
                            byte_cnt <= byte_cnt + 1'b1;
                        end
                    end
                    //---------------------------------------------------------
                    S_SIDE: begin
                        r_side <= rx_data;
                        state  <= S_TSTAMP;
                        byte_cnt <= '0;
                    end
                    //---------------------------------------------------------
                    S_TSTAMP: begin
                        r_tstamp <= { r_tstamp[55:0], rx_data };
                        if (byte_cnt == 3'd7) begin
                            state    <= S_EOF;
                            byte_cnt <= '0;
                        end else begin
                            byte_cnt <= byte_cnt + 1'b1;
                        end
                    end
                    //---------------------------------------------------------
                    // Good EOF: latch outputs, pulse msg_valid.
                    // Bad EOF: pulse parse_error, outputs untouched.
                    // Either way, back to IDLE on the same clock edge —
                    // zero dead cycles before the next SOF is accepted.
                    S_EOF: begin
                        if (rx_data == EOF_BYTE) begin
                            msg_type    <= r_type;
                            symbol      <= r_symbol;
                            price       <= r_price;
                            qty         <= r_qty;
                            side        <= r_side;
                            host_tstamp <= r_tstamp;
                            msg_valid   <= 1'b1;
                        end else begin
                            parse_error <= 1'b1;
                        end
                        state <= S_IDLE;
                    end
                endcase
            end
        end
    end

endmodule

`default_nettype wire
