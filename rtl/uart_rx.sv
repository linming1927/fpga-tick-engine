//-----------------------------------------------------------------------------
// uart_rx.sv — UART receiver, 8N1
//
// Converts the raw UART serial bit-stream into a byte-at-a-time interface.
// See MANUAL.md §3 for the full behavioral description.
//
//   IDLE  : wait for falling edge on rx (start bit) via 2-FF synchronizer
//   START : wait CLKS_PER_BIT/2, re-sample; low = genuine start, high = glitch
//   DATA  : sample once per CLKS_PER_BIT for 8 bits, LSB first
//   STOP  : sample stop bit; high -> rx_valid, low -> rx_error (framing)
//-----------------------------------------------------------------------------
`timescale 1ns / 1ps
`default_nettype none

module uart_rx #(
    parameter int CLK_HZ = 100_000_000,
    parameter int BAUD   = 115_200
)(
    input  wire  logic       clk,       // system clock
    input  wire  logic       rst_n,     // active-low synchronous reset
    input  wire  logic       rx,        // async UART RX line from host (idle = 1)
    output logic [7:0] rx_data,   // received byte; stable when rx_valid pulses
    output logic       rx_valid,  // 1-cycle pulse: new byte ready on rx_data
    output logic       rx_error   // 1-cycle pulse: framing error (stop bit low)
);

    localparam int CLKS_PER_BIT = CLK_HZ / BAUD;   // 868 @ 100 MHz / 115200
    localparam int HALF_BIT     = CLKS_PER_BIT / 2;
    localparam int CNT_W        = $clog2(CLKS_PER_BIT);

    //-------------------------------------------------------------------------
    // 2-FF synchronizer — rx is asynchronous to clk; this prevents
    // metastability from propagating into the FSM.
    //-------------------------------------------------------------------------
    logic rx_meta, rx_sync;
    always_ff @(posedge clk) begin
        rx_meta <= rx;
        rx_sync <= rx_meta;
    end

    typedef enum logic [1:0] { S_IDLE, S_START, S_DATA, S_STOP } state_t;
    state_t state = S_IDLE;

    logic [CNT_W-1:0] clk_cnt;
    logic [2:0]       bit_idx;
    logic [7:0]       shift_reg;

    always_ff @(posedge clk) begin
        if (!rst_n) begin
            state     <= S_IDLE;
            clk_cnt   <= '0;
            bit_idx   <= '0;
            shift_reg <= '0;
            rx_data   <= '0;
            rx_valid  <= 1'b0;
            rx_error  <= 1'b0;
        end else begin
            // strobes are single-cycle pulses
            rx_valid <= 1'b0;
            rx_error <= 1'b0;

            unique case (state)
                //-------------------------------------------------------------
                S_IDLE: begin
                    clk_cnt <= '0;
                    bit_idx <= '0;
                    if (rx_sync == 1'b0)          // falling edge = start bit
                        state <= S_START;
                end
                //-------------------------------------------------------------
                // Wait half a bit period, re-sample at the midpoint of the
                // start bit. This aligns all later samples to bit centres.
                S_START: begin
                    if (clk_cnt == HALF_BIT - 1) begin
                        clk_cnt <= '0;
                        state   <= (rx_sync == 1'b0) ? S_DATA : S_IDLE; // glitch reject
                    end else begin
                        clk_cnt <= clk_cnt + 1'b1;
                    end
                end
                //-------------------------------------------------------------
                // Sample 8 data bits, one per full bit period, LSB first
                // (standard UART: bit 0 of the byte arrives first on the wire).
                S_DATA: begin
                    if (clk_cnt == CLKS_PER_BIT - 1) begin
                        clk_cnt   <= '0;
                        shift_reg <= { rx_sync, shift_reg[7:1] };
                        if (bit_idx == 3'd7)
                            state <= S_STOP;
                        else
                            bit_idx <= bit_idx + 1'b1;
                    end else begin
                        clk_cnt <= clk_cnt + 1'b1;
                    end
                end
                //-------------------------------------------------------------
                // One more bit period lands us mid-stop-bit. High = good byte,
                // low = framing error (rx_data NOT updated).
                S_STOP: begin
                    if (clk_cnt == CLKS_PER_BIT - 1) begin
                        clk_cnt <= '0;
                        if (rx_sync) begin
                            rx_data  <= shift_reg;
                            rx_valid <= 1'b1;
                        end else begin
                            rx_error <= 1'b1;
                        end
                        state <= S_IDLE;
                    end else begin
                        clk_cnt <= clk_cnt + 1'b1;
                    end
                end
            endcase
        end
    end

endmodule

`default_nettype wire
