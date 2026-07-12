//-----------------------------------------------------------------------------
// tb_indicator_e2e.sv — end-to-end: UART frames in, 0x83 signal frame out
//
// The unit bench (tb_indicator.sv) proves the engine's math; this bench
// proves the plumbing: host frame -> uart_rx -> tick_parser -> tick_* bus ->
// indicator_engine -> signal FIFO -> priority arbitration -> frame_tx ->
// uart_tx -> host, with the SMAs riding home inside the signal frame.
//
// Windows overridden to FAST_N=4 / SLOW_N=8. Price script (values are
// price x 10000, i.e. dollars x 10000):
//   8 warm-up SPY trades descending 2000..1300  -> primes state, fast<slow
//   1 AAPL trade interleaved                    -> must be ignored by engine
//   SPY trade @3000                             -> golden cross, exactly one
//   SPY trade @3100                             -> still above, no retrigger
//
// Expected on the wire: 11 echo frames (10 SPY + 1 AAPL) + exactly one
// 0x83 signal frame with SIDE=BUY, PRICE=3000, and SMA_FAST > SMA_SLOW
// matching hand-computed integer values (fast: (1500+1400+1300+3000)>>2 =
// 1800; slow: (2000..1300 sum - 2000 + 3000)>>3 = 1775 — in x10000 units).
//
// Run:  iverilog -g2012 -o sime2e.out sim/tb_indicator_e2e.sv rtl/*.sv
//       vvp sime2e.out
//-----------------------------------------------------------------------------
`timescale 1ns / 1ps

module tb_indicator_e2e;

    localparam int  CLK_HZ     = 100_000_000;
    localparam int  BAUD       = 115_200;
    localparam real CLK_PERIOD = 10.0;
    localparam real BIT_NS     = 1e9 / BAUD;

    logic       clk100 = 1'b0;
    logic       ck_rst;
    logic       uart_txd_in = 1'b1;
    logic       uart_rxd_out;
    logic [3:0] led;

    always #(CLK_PERIOD/2.0) clk100 = ~clk100;

    top_arty #(
        .CLK_HZ        ( CLK_HZ ),
        .BAUD          ( BAUD   ),
        .TARGET_SYMBOL ( "SPY " ),
        .FAST_N        ( 4      ),
        .SLOW_N        ( 8      ),
        .EMA_KF        ( 1      ),
        .EMA_KS        ( 3      ),
        .EMA_WARMUP    ( 8      )
    ) dut (
        .clk100       ( clk100       ),
        .ck_rst       ( ck_rst       ),
        .uart_txd_in  ( uart_txd_in  ),
        .uart_rxd_out ( uart_rxd_out ),
        .led          ( led          )
    );

    // ---- scoreboard ----------------------------------------------------------
    int pass_count = 0;
    int fail_count = 0;

    task automatic check64(input string name, input logic [63:0] got,
                           input logic [63:0] exp);
        if (got === exp) begin
            pass_count++;
            $display("  PASS  %-28s = 0x%h", name, got);
        end else begin
            fail_count++;
            $display("  FAIL  %-28s = 0x%h (expected 0x%h)", name, got, exp);
        end
    endtask

    // ---- UART driver -----------------------------------------------------------
    task automatic uart_send_byte(input logic [7:0] b);
        uart_txd_in = 1'b0;
        #(BIT_NS);
        for (int i = 0; i < 8; i++) begin
            uart_txd_in = b[i];
            #(BIT_NS);
        end
        uart_txd_in = 1'b1;
        #(BIT_NS);
    endtask

    localparam logic [63:0] HOST_TS_CONST = 64'd1_750_000_000_000_000;

    task automatic send_trade(input logic [31:0] fsym, input logic [31:0] fprice,
                              input logic [15:0] fqty);
        uart_send_byte(8'hAA);
        uart_send_byte(8'h01);
        for (int i = 3; i >= 0; i--) uart_send_byte(fsym[8*i +: 8]);
        for (int i = 3; i >= 0; i--) uart_send_byte(fprice[8*i +: 8]);
        for (int i = 1; i >= 0; i--) uart_send_byte(fqty[8*i +: 8]);
        uart_send_byte(8'h01);
        for (int i = 7; i >= 0; i--) uart_send_byte(HOST_TS_CONST[8*i +: 8]);
        uart_send_byte(8'h55);
    endtask

    // ---- UART monitor ------------------------------------------------------------
    logic [7:0] echo_bytes [0:4095];
    int         echo_count = 0;
    logic [7:0] mon_byte;

    initial begin
        #(BIT_NS);
        forever begin
            @(negedge uart_rxd_out);
            #(BIT_NS * 0.5);
            if (uart_rxd_out === 1'b0) begin
                mon_byte = '0;
                for (int i = 0; i < 8; i++) begin
                    #(BIT_NS);
                    mon_byte[i] = uart_rxd_out;
                end
                #(BIT_NS);
                if (uart_rxd_out === 1'b1) begin
                    echo_bytes[echo_count] = mon_byte;
                    echo_count++;
                end
            end
        end
    end

    function automatic logic [63:0] fld(input int frame, input int offset,
                                        input int nbytes);
        logic [63:0] v;
        v = '0;
        for (int i = 0; i < nbytes; i++)
            v = (v << 8) | echo_bytes[frame*30 + offset + i];
        return v;
    endfunction

    task automatic wait_drain;
        int prev;
        prev = -1;
        while (echo_count != prev) begin
            prev = echo_count;
            #(3_000_000);
        end
    endtask

    // ---- sequence -----------------------------------------------------------------
    int n_frames, n_sig, sig_f, n_echo, n_ema, ema_f;

    initial begin
        $dumpfile("tb_indicator_e2e.vcd");
        $dumpvars(0, tb_indicator_e2e);

        ck_rst = 1'b0;
        repeat (20) @(posedge clk100);
        ck_rst = 1'b1;
        repeat (40) @(posedge clk100);

        $display("\n[E2E] warm-up: 8 descending SPY trades (+1 AAPL interloper)");
        send_trade("SPY ", 32'd2000, 16'd1);
        send_trade("SPY ", 32'd1900, 16'd2);
        send_trade("SPY ", 32'd1800, 16'd3);
        send_trade("SPY ", 32'd1700, 16'd4);
        send_trade("AAPL", 32'd9999, 16'd5);   // engine must ignore this one
        send_trade("SPY ", 32'd1600, 16'd6);
        send_trade("SPY ", 32'd1500, 16'd7);
        send_trade("SPY ", 32'd1400, 16'd8);
        send_trade("SPY ", 32'd1300, 16'd9);

        $display("[E2E] spike to force the golden cross, then hold above");
        send_trade("SPY ", 32'd3000, 16'd10);
        send_trade("SPY ", 32'd3100, 16'd11);

        wait_drain();

        //-----------------------------------------------------------------------
        n_frames = echo_count / 30;
        n_sig    = 0;  sig_f = -1;
        n_ema    = 0;  ema_f = -1;
        n_echo   = 0;
        for (int f = 0; f < n_frames; f++) begin
            if      (fld(f, 1, 1) == 64'h83) begin n_sig++; sig_f = f; end
            else if (fld(f, 1, 1) == 64'h84) begin n_ema++; ema_f = f; end
            else                             n_echo++;
        end
        $display("\n  INFO  %0d frames on the wire: %0d echoes, %0d signal(s)",
                 n_frames, n_echo, n_sig);

        check64("all bytes framed",       echo_count % 30, 64'd0);
        check64("echo count (11 ticks)",  n_echo,          64'd11);
        check64("exactly one 0x83",       n_sig,           64'd1);
        check64("exactly one 0x84",       n_ema,           64'd1);
        check64("no echo drops",  {48'd0, dut.tx_drop_count},  64'd0);
        check64("no signal drops",{48'd0, dut.sig_drop_count}, 64'd0);

        if (sig_f >= 0) begin
            check64("sig SOF",        fld(sig_f,  0, 1), 64'hBB);
            check64("sig SYMBOL",     fld(sig_f,  2, 4), 64'h5350_5920); // "SPY "
            check64("sig PRICE",      fld(sig_f,  6, 4), 64'd3000);
            check64("sig QTY==0",     fld(sig_f, 10, 2), 64'd0);
            check64("sig SIDE==BUY",  fld(sig_f, 12, 1), 64'h01);
            check64("sig SMA_FAST",   fld(sig_f, 13, 4), 64'd1800);  // hand-computed
            check64("sig SMA_SLOW",   fld(sig_f, 17, 4), 64'd1775);  // hand-computed
            check64("sig EOF",        fld(sig_f, 29, 1), 64'hCC);
            if (fld(sig_f, 13, 4) > fld(sig_f, 17, 4)) begin
                pass_count++;
                $display("  PASS  SMA_FAST > SMA_SLOW inside signal frame");
            end else begin
                fail_count++;
                $display("  FAIL  SMA ordering wrong in signal frame");
            end
        end

        // The 3000 spike crosses BOTH strategies on the SAME tick, so this
        // also exercises the two-writer collision arbiter: the SMA record
        // is written first, the EMA record via the pend register.
        if (ema_f >= 0) begin
            check64("ema SOF",       fld(ema_f,  0, 1), 64'hBB);
            check64("ema SYMBOL",    fld(ema_f,  2, 4), 64'h5350_5920);
            check64("ema PRICE",     fld(ema_f,  6, 4), 64'd3000);
            check64("ema SIDE==BUY", fld(ema_f, 12, 1), 64'h01);
            check64("ema FAST anchor", fld(ema_f, 13, 4), 64'd2200);
            check64("ema SLOW anchor", fld(ema_f, 17, 4), 64'd1884);
            check64("ema EOF",       fld(ema_f, 29, 1), 64'hCC);
            check64("collision order: SMA frame precedes EMA",
                    sig_f < ema_f, 64'd1);
        end

        //-----------------------------------------------------------------------
        $display("\n==============================================");
        $display("  RESULT: %0d PASS / %0d FAIL", pass_count, fail_count);
        $display("==============================================\n");
        if (fail_count == 0) $display("ALL TESTS PASSED");
        else                 $display("*** FAILURES DETECTED ***");
        $finish;
    end

    initial begin
        #200_000_000;
        $display("*** TIMEOUT ***");
        $finish;
    end

endmodule
