//-----------------------------------------------------------------------------
// uart_tx.sv — UART transmitter, 8N1
//
// The structural mirror of uart_rx: where the receiver *samples* the line at
// predicted bit centers, the transmitter *drives* the line for whole bit
// periods. Driving is the easier half — there is no clock recovery problem,
// no synchronizer, no glitch rejection. We own the timing.
//
// Handshake: valid/ready. tx_ready is high exactly when the module is idle.
// A transfer happens on any clock edge where (tx_valid && tx_ready); the
// byte is latched and transmission begins on the next cycle. The upstream
// producer (frame_tx) holds tx_valid high until the transfer occurs — the
// standard AXI-Stream-style discipline, chosen here because a byte takes
// ~8680 cycles to send and a pulse-style handshake would force the producer
// to re-time its pulses to the transmitter's schedule.
//
// tx is a REGISTERED output: it changes only on clock edges, never glitches
// from combinational decode — important on a wire leaving the chip.
//-----------------------------------------------------------------------------
`timescale 1ns / 1ps
`default_nettype none

module uart_tx #(
    parameter int CLK_HZ = 100_000_000,
    parameter int BAUD   = 115_200
)(
    input  wire  logic       clk,
    input  wire  logic       rst_n,     // active-low synchronous reset

    input  wire  logic [7:0] tx_data,   // byte to send
    input  wire  logic       tx_valid,  // producer has a byte
    output logic       tx_ready,  // high in IDLE; transfer on valid&&ready

    output logic       tx         // UART TX line to host (idle = 1)
);

    localparam int CLKS_PER_BIT = CLK_HZ / BAUD;   // 868 @ 100 MHz / 115200
    localparam int CNT_W        = $clog2(CLKS_PER_BIT);

    typedef enum logic [1:0] { S_IDLE, S_START, S_DATA, S_STOP } state_t;
    state_t state = S_IDLE;

    logic [CNT_W-1:0] clk_cnt;
    logic [2:0]       bit_idx;
    logic [7:0]       data_reg;

    // ready is purely "am I idle" — combinational, so a waiting producer
    // sees ready fall the cycle after a transfer is accepted
    assign tx_ready = (state == S_IDLE);

    always_ff @(posedge clk) begin
        if (!rst_n) begin
            state    <= S_IDLE;
            clk_cnt  <= '0;
            bit_idx  <= '0;
            data_reg <= '0;
            tx       <= 1'b1;          // UART line idles high
        end else begin
            unique case (state)
                //-------------------------------------------------------------
                S_IDLE: begin
                    tx      <= 1'b1;
                    clk_cnt <= '0;
                    bit_idx <= '0;
                    if (tx_valid) begin           // tx_ready is implied here
                        data_reg <= tx_data;
                        state    <= S_START;
                    end
                end
                //-------------------------------------------------------------
                // Drive the start bit (low) for one full bit period.
                S_START: begin
                    tx <= 1'b0;
                    if (clk_cnt == CLKS_PER_BIT - 1) begin
                        clk_cnt <= '0;
                        state   <= S_DATA;
                    end else begin
                        clk_cnt <= clk_cnt + 1'b1;
                    end
                end
                //-------------------------------------------------------------
                // Drive 8 data bits, LSB first — the same convention uart_rx
                // assumes when it right-shifts received bits (walkthrough
                // §3.4). data_reg[bit_idx] indexes bit 0 first.
                S_DATA: begin
                    tx <= data_reg[bit_idx];
                    if (clk_cnt == CLKS_PER_BIT - 1) begin
                        clk_cnt <= '0;
                        if (bit_idx == 3'd7)
                            state <= S_STOP;
                        else
                            bit_idx <= bit_idx + 1'b1;
                    end else begin
                        clk_cnt <= clk_cnt + 1'b1;
                    end
                end
                //-------------------------------------------------------------
                // Drive the stop bit (high) for one full bit period, then
                // return to IDLE. Back-to-back bytes therefore get exactly
                // one stop bit — the minimum 8N1 allows, maximizing
                // throughput on the echo path.
                S_STOP: begin
                    tx <= 1'b1;
                    if (clk_cnt == CLKS_PER_BIT - 1) begin
                        clk_cnt <= '0;
                        state   <= S_IDLE;
                    end else begin
                        clk_cnt <= clk_cnt + 1'b1;
                    end
                end
            endcase
        end
    end

endmodule

`default_nettype wire
