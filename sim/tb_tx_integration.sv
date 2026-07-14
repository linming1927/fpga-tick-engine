//-----------------------------------------------------------------------------
// tb_tx_integration.sv — bit-level test of the full RX -> FIFO -> TX echo path
//
// The bench now contains a UART *monitor*: a behavioral receiver that watches
// uart_rxd_out, recovers bytes exactly the way uart_rx does (half-bit align,
// center sampling), and appends them to echo_bytes[]. Checks then decode
// whole 30-byte echo frames out of that array.
//
// The DUT is instantiated with TX_FIFO_DEPTH=2 (hardware default is 16) so
// the overflow/drop path is reachable in a reasonable simulation time: with
// 22-byte input frames (~1.91 ms) vs 30-byte echoes (~2.60 ms), back-to-back
// input outruns the echo path by ~0.69 ms per frame.
//
// Tests:
//   T1  One valid frame -> one echo; every field checked, incl. FPGA_TS ==
//       the arrival timestamp committed at that frame's msg_valid (exact)
//   T2  Second frame -> second echo; FPGA_TS advanced by ~ one frame time
//   T3  Corrupt frame (bad EOF) -> NO echo emitted (byte count unchanged)
//   T4  Burst of 12 back-to-back frames, qty = 1..12:
//         - invariant: echo_frames + tx_drop_count == 12
//         - at least one drop occurred (proves overflow path exercised)
//         - every received echo is well-formed (SOF/TYPE/EOF/symbol)
//         - qty values across echoes are strictly increasing (order kept,
//           drops only ever remove from the tail-pressure, never reorder)
//
// Run:  iverilog -g2012 -o simtx.out sim/tb_tx_integration.sv rtl/*.sv
//       vvp simtx.out
//-----------------------------------------------------------------------------
`timescale 1ns / 1ps

module tb_tx_integration;

    localparam int  CLK_HZ     = 100_000_000;
    localparam int  BAUD       = 1_152_000;  // 10x sim speed: TB bit period
                                             // and DUT param both derive
                                             // from this one constant, so
                                             // the timing relationship is
                                             // preserved (86 clk/bit)
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
        .TX_FIFO_DEPTH ( 2      )     // small on purpose — see header
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
            $display("  PASS  %-26s = 0x%h", name, got);
        end else begin
            fail_count++;
            $display("  FAIL  %-26s = 0x%h (expected 0x%h)", name, got, exp);
        end
    endtask

    // ---- stimulus side: same UART driver as tb_top_integration ----------------
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

    task automatic send_frame(
        input logic [7:0]  ftype,
        input logic [47:0] fsym,
        input logic [31:0] fprice,
        input logic [15:0] fqty,
        input logic [7:0]  fside,
        input logic [63:0] ftstamp,
        input logic [7:0]  feof
    );
        uart_send_byte(8'hAA);
        uart_send_byte(ftype);
        for (int i = 5; i >= 0; i--) uart_send_byte(fsym[8*i +: 8]);
        for (int i = 3; i >= 0; i--) uart_send_byte(fprice[8*i +: 8]);
        for (int i = 1; i >= 0; i--) uart_send_byte(fqty[8*i +: 8]);
        uart_send_byte(fside);
        for (int i = 7; i >= 0; i--) uart_send_byte(ftstamp[8*i +: 8]);
        uart_send_byte(feof);
    endtask

    // ---- monitor side: behavioral UART receiver on the FPGA's TX line ---------
    logic [7:0] echo_bytes [0:2047];
    int         echo_count = 0;
    logic [7:0] mon_byte;

    initial begin
        #(BIT_NS);                          // let the line settle out of X
        forever begin
            @(negedge uart_rxd_out);        // candidate start bit
            #(BIT_NS * 0.5);
            if (uart_rxd_out === 1'b0) begin       // still low at midpoint
                mon_byte = '0;
                for (int i = 0; i < 8; i++) begin
                    #(BIT_NS);
                    mon_byte[i] = uart_rxd_out;    // LSB first
                end
                #(BIT_NS);                          // mid stop bit
                if (uart_rxd_out === 1'b1) begin
                    echo_bytes[echo_count] = mon_byte;
                    echo_count++;
                end else begin
                    $display("  MONITOR: framing error on FPGA TX at %0t", $time);
                end
            end
        end
    end

    // decode a big-endian field out of a captured echo frame
    function automatic logic [63:0] fld(input int frame, input int offset,
                                        input int nbytes);
        logic [63:0] v;
        v = '0;
        for (int i = 0; i < nbytes; i++)
            v = (v << 8) | echo_bytes[frame*32 + offset + i];
        return v;
    endfunction

    // ---- arrival-timestamp reference: capture the value timestamp_us commits
    //      at each msg_valid (pre-edge 'pending' == the committed value) -------
    logic [63:0] arrivals [0:31];
    int          mv_count = 0;

    always @(posedge clk100) begin
        if (dut.msg_valid) begin
            arrivals[mv_count] = dut.u_timestamp.pending;
            mv_count++;
        end
    end

    // ---- helper: wait until the echo stream has been quiet for 3 ms -----------
    task automatic wait_drain;
        int prev;
        prev = -1;
        while (echo_count != prev) begin
            prev = echo_count;
            #(3_000_000);   // 3 ms of silence = FIFO drained, TX idle
        end
    endtask

    // ---- stimulus sequence -----------------------------------------------------
    logic [63:0] t1_fpga_ts;
    int          t4_frames;
    logic [63:0] q_prev, q_now;
    bit          ok;

    initial begin
        $dumpfile("tb_tx_integration.vcd");
        $dumpvars(0, tb_tx_integration);

        ck_rst = 1'b0;
        repeat (20) @(posedge clk100);
        ck_rst = 1'b1;
        repeat (40) @(posedge clk100);

        //-----------------------------------------------------------------------
        $display("\n[T1] One valid frame -> one fully-checked echo");
        send_frame(8'h01, "AAPL  ", 32'd1_823_400, 16'd100, 8'h01,
                   64'd1_750_000_000_123_456, 8'h55);
        wait (echo_count >= 32);
        #(BIT_NS * 3);

        check64("echo SOF",      fld(0,  0, 1), 64'hBB);
        check64("echo TYPE",     fld(0,  1, 1), 64'h81);           // 0x80|trade
        check64("echo SYMBOL",   fld(0,  2, 4), 64'h4141_504C);    // "AAPL  "
        check64("echo PRICE",    fld(0,  8, 4), 64'd1_823_400);
        check64("echo QTY",      fld(0, 12, 2), 64'd100);
        check64("echo SIDE",     fld(0, 14, 1), 64'h01);
        check64("echo HOST_TS",  fld(0, 15, 8), 64'd1_750_000_000_123_456);
        t1_fpga_ts = fld(0, 23, 8);
        check64("echo FPGA_TS",  t1_fpga_ts,    arrivals[0]);      // exact
        check64("echo EOF",      fld(0, 31, 1), 64'hCC);
        check64("byte count",    echo_count,    64'd32);

        //-----------------------------------------------------------------------
        $display("\n[T2] Second frame -> second echo, arrival advanced");
        send_frame(8'h02, "TSLA  ", 32'd2_489_900, 16'd250, 8'h02,
                   64'd1_750_000_001_000_000, 8'h55);
        wait (echo_count >= 64);
        #(BIT_NS * 3);

        check64("echo TYPE",    fld(1,  1, 1), 64'h82);            // 0x80|quote
        check64("echo SYMBOL",  fld(1,  2, 4), 64'h5453_4C41);     // "TSLA  "
        check64("echo FPGA_TS", fld(1, 23, 8), arrivals[1]);
        if (fld(1, 23, 8) > t1_fpga_ts) begin
            pass_count++;
            $display("  PASS  FPGA_TS advanced: %0d -> %0d us (delta %0d us)",
                     t1_fpga_ts, fld(1,23,8), fld(1,23,8) - t1_fpga_ts);
        end else begin
            fail_count++;
            $display("  FAIL  FPGA_TS did not advance");
        end

        //-----------------------------------------------------------------------
        $display("\n[T3] Corrupt frame -> NO echo");
        send_frame(8'h01, "NVDA  ", 32'd9_999_999, 16'd1, 8'h01,
                   64'd1_750_000_002_000_000, 8'hFF);   // bad EOF
        #(6_000_000);                                    // 6 ms: > frame + echo
        check64("byte count unchanged", echo_count, 64'd64);
        check64("parse_error count", {48'd0, dut.parse_error_count}, 64'd1);

        //-----------------------------------------------------------------------
        $display("\n[T4] Burst of 12 back-to-back frames into a depth-2 FIFO");
        for (int k = 1; k <= 12; k++) begin
            send_frame(8'h01, "SPY   ", 32'd5_000_000 + k, 16'(k), 8'h01,
                       64'd1_750_000_003_000_000 + k, 8'h55);
        end
        wait_drain();

        t4_frames = (echo_count - 64) / 32;
        $display("  INFO  %0d of 12 burst frames echoed, tx_drop_count = %0d",
                 t4_frames, dut.tx_drop_count);

        check64("all bytes framed (count %% 32)", echo_count % 32, 64'd0);
        check64("echoes + drops == ticks",
                t4_frames + dut.tx_drop_count, 64'd12);
        if (dut.tx_drop_count >= 1) begin
            pass_count++;
            $display("  PASS  overflow path exercised (>=1 drop)");
        end else begin
            fail_count++;
            $display("  FAIL  no drops — FIFO never overflowed; test ineffective");
        end

        // well-formedness + strict qty ordering across all T4 echoes
        ok = 1'b1;
        q_prev = 0;
        for (int f = 2; f < 2 + t4_frames; f++) begin
            if (fld(f, 0, 1) != 64'hBB || fld(f, 31, 1) != 64'hCC ||
                fld(f, 1, 1) != 64'h81 || fld(f, 2, 6) != 64'h5350_5920_2020) begin
                ok = 1'b0;
                $display("  FAIL  echo frame %0d malformed", f);
            end
            q_now = fld(f, 12, 2);
            if (q_now <= q_prev) begin
                ok = 1'b0;
                $display("  FAIL  qty not strictly increasing at frame %0d", f);
            end
            q_prev = q_now;
        end
        if (ok) begin
            pass_count++;
            $display("  PASS  all %0d echoes well-formed, in order", t4_frames);
        end else begin
            fail_count++;
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
        #200_000_000;   // 200 ms
        $display("*** TIMEOUT — simulation hung ***");
        $finish;
    end

endmodule
