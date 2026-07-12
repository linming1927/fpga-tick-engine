//-----------------------------------------------------------------------------
// tb_top_integration.sv — bit-level integration test of top_arty
//
// Unlike tb_tick_parser (byte-level FSM test), this bench drives the actual
// UART serial waveform — start bit, 8 data bits LSB-first, stop bit — at
// 115 200 baud into uart_txd_in, through the real uart_rx, tick_parser and
// timestamp_us instances inside top_arty.
//
// Tests:
//   1  Valid trade  AAPL $182.34 x 100 BUY   — all fields decode
//   2  FPGA arrival timestamp of T1 latched at SOF (±2 us tolerance)
//   3  Valid quote  TSLA $248.99 x 250 SELL  — fields decode; arrival advances
//   4  Corrupted EOF (0xFF)                  — error counter increments,
//                                              LED0 lights, outputs still T3's
//   5  Recovery: valid SPY frame right after the bad one decodes cleanly
//   6  Garbage bytes (incl. stray 0xAA) then valid MSFT frame — resync
//
// Run:  iverilog -g2012 -o sim.out sim/tb_top_integration.sv \
//           rtl/top_arty.sv rtl/uart_rx.sv rtl/tick_parser.sv rtl/timestamp_us.sv
//       vvp sim.out
//-----------------------------------------------------------------------------
`timescale 1ns / 1ps

module tb_top_integration;

    // ---- clock / DUT ---------------------------------------------------------
    localparam int CLK_HZ      = 100_000_000;
    localparam int BAUD        = 115_200;
    localparam real CLK_PERIOD = 10.0;                       // ns
    localparam real BIT_NS     = 1e9 / BAUD;                 // 8680.6 ns / bit

    logic       clk100 = 1'b0;
    logic       ck_rst;
    logic       uart_txd_in = 1'b1;   // idle high
    logic [3:0] led;

    always #(CLK_PERIOD/2.0) clk100 = ~clk100;

    top_arty #(
        .CLK_HZ ( CLK_HZ ),
        .BAUD   ( BAUD   )
    ) dut (
        .clk100      ( clk100      ),
        .ck_rst      ( ck_rst      ),
        .uart_txd_in ( uart_txd_in ),
        .led         ( led         )
    );

    // ---- scoreboard ----------------------------------------------------------
    int pass_count = 0;
    int fail_count = 0;

    task automatic check64(input string name, input logic [63:0] got,
                           input logic [63:0] exp);
        if (got === exp) begin
            pass_count++;
            $display("  PASS  %-22s = 0x%h", name, got);
        end else begin
            fail_count++;
            $display("  FAIL  %-22s = 0x%h (expected 0x%h)", name, got, exp);
        end
    endtask

    task automatic check_near_us(input string name, input logic [63:0] got,
                                 input logic [63:0] exp, input int tol_us);
        if ((got >= exp - tol_us) && (got <= exp + tol_us)) begin
            pass_count++;
            $display("  PASS  %-22s = %0d us (expected %0d ±%0d)", name, got, exp, tol_us);
        end else begin
            fail_count++;
            $display("  FAIL  %-22s = %0d us (expected %0d ±%0d)", name, got, exp, tol_us);
        end
    endtask

    // ---- UART bit-level driver ------------------------------------------------
    task automatic uart_send_byte(input logic [7:0] b);
        uart_txd_in = 1'b0;                        // start bit
        #(BIT_NS);
        for (int i = 0; i < 8; i++) begin          // data, LSB first
            uart_txd_in = b[i];
            #(BIT_NS);
        end
        uart_txd_in = 1'b1;                        // stop bit
        #(BIT_NS);
    endtask

    // sends one full 22-byte frame. sof_us_captured is grabbed by the monitor
    // below at the exact clock edge the parser pulses sof_seen — the same
    // edge on which timestamp_us latches its pending value.
    logic [63:0] sof_us_captured;

    always @(posedge clk100)
        if (dut.sof_seen) sof_us_captured <= dut.u_timestamp.us_now;

    task automatic send_frame(
        input logic [7:0]  ftype,
        input logic [31:0] fsym,
        input logic [31:0] fprice,
        input logic [15:0] fqty,
        input logic [7:0]  fside,
        input logic [63:0] ftstamp,
        input logic [7:0]  feof        // pass 8'h55 for good, anything else = bad
    );
        uart_send_byte(8'hAA);                       // SOF
        uart_send_byte(ftype);
        for (int i = 3; i >= 0; i--) uart_send_byte(fsym[8*i +: 8]);
        for (int i = 3; i >= 0; i--) uart_send_byte(fprice[8*i +: 8]);
        for (int i = 1; i >= 0; i--) uart_send_byte(fqty[8*i +: 8]);
        uart_send_byte(fside);
        for (int i = 7; i >= 0; i--) uart_send_byte(ftstamp[8*i +: 8]);
        uart_send_byte(feof);
        #(BIT_NS * 3);                               // idle gap
    endtask

    // ---- msg_valid / parse_error pulse monitors --------------------------------
    int msg_valid_seen   = 0;
    int parse_error_seen = 0;
    always @(posedge clk100) begin
        if (dut.msg_valid)   msg_valid_seen++;
        if (dut.parse_error) parse_error_seen++;
    end

    // ---- stimulus ---------------------------------------------------------------
    logic [63:0] arrival_t1;

    initial begin
        $dumpfile("tb_top_integration.vcd");
        $dumpvars(0, tb_top_integration);

        // reset
        ck_rst = 1'b0;
        repeat (20) @(posedge clk100);
        ck_rst = 1'b1;
        repeat (40) @(posedge clk100);

        //-----------------------------------------------------------------------
        $display("\n[T1] Valid trade: AAPL $182.34 x 100 BUY");
        send_frame(8'h01, "AAPL", 32'd1_823_400, 16'd100, 8'h01,
                   64'd1_750_000_000_123_456, 8'h55);
        repeat (10) @(posedge clk100);

        check64("msg_type",    {56'd0, dut.msg_type},   64'h01);
        check64("symbol",      {32'd0, dut.symbol},     {32'd0, 32'h4141_504C});
        check64("price",       {32'd0, dut.price},      64'd1_823_400);
        check64("qty",         {48'd0, dut.qty},        64'd100);
        check64("side",        {56'd0, dut.side},       64'h01);
        check64("host_tstamp", dut.host_tstamp,         64'd1_750_000_000_123_456);
        check64("msg_valid count",   msg_valid_seen,    64'd1);
        check64("parse_error count", parse_error_seen,  64'd0);

        //-----------------------------------------------------------------------
        $display("\n[T2] FPGA arrival timestamp latched at SOF of T1");
        arrival_t1 = dut.u_timestamp.arrival_us;
        check_near_us("arrival_us", arrival_t1, sof_us_captured, 1);

        //-----------------------------------------------------------------------
        $display("\n[T3] Valid quote: TSLA $248.99 x 250 SELL");
        send_frame(8'h02, "TSLA", 32'd2_489_900, 16'd250, 8'h02,
                   64'd1_750_000_001_000_000, 8'h55);
        repeat (10) @(posedge clk100);

        check64("msg_type", {56'd0, dut.msg_type}, 64'h02);
        check64("symbol",   {32'd0, dut.symbol},   {32'd0, 32'h54534C41});
        check64("price",    {32'd0, dut.price},    64'd2_489_900);
        check64("qty",      {48'd0, dut.qty},      64'd250);
        check64("side",     {56'd0, dut.side},     64'h02);
        check_near_us("arrival_us (T3 SOF)", dut.u_timestamp.arrival_us,
                      sof_us_captured, 1);
        if (dut.u_timestamp.arrival_us > arrival_t1) begin
            pass_count++;
            $display("  PASS  arrival advanced: %0d us -> %0d us (delta %0d us)",
                     arrival_t1, dut.u_timestamp.arrival_us,
                     dut.u_timestamp.arrival_us - arrival_t1);
        end else begin
            fail_count++;
            $display("  FAIL  arrival timestamp did not advance");
        end

        //-----------------------------------------------------------------------
        $display("\n[T4] Corrupted EOF (0xFF): error counted, outputs held at T3");
        send_frame(8'h01, "NVDA", 32'd9_999_999, 16'd1, 8'h01,
                   64'd1_750_000_002_000_000, 8'hFF);        // bad EOF
        repeat (10) @(posedge clk100);

        check64("parse_error count", parse_error_seen,      64'd1);
        check64("error counter reg", {48'd0, dut.parse_error_count}, 64'd1);
        check64("symbol still TSLA", {32'd0, dut.symbol},   {32'd0, 32'h54534C41});
        check64("price still TSLA",  {32'd0, dut.price},    64'd2_489_900);
        if (led[0] === 1'b1) begin
            pass_count++;  $display("  PASS  LED0 lit on parse_error (stretched)");
        end else begin
            fail_count++;  $display("  FAIL  LED0 not lit after parse_error");
        end

        //-----------------------------------------------------------------------
        $display("\n[T5] Recovery: valid SPY frame immediately after the bad one");
        send_frame(8'h01, "SPY ", 32'd5_000_000, 16'd500, 8'h01,
                   64'd1_750_000_003_000_000, 8'h55);
        repeat (10) @(posedge clk100);

        check64("symbol",  {32'd0, dut.symbol}, {32'd0, 32'h53505920});
        check64("price",   {32'd0, dut.price},  64'd5_000_000);
        check64("qty",     {48'd0, dut.qty},    64'd500);
        check64("msg_valid count", msg_valid_seen, 64'd3);

        //-----------------------------------------------------------------------
        $display("\n[T6] Garbage (incl. stray 0xAA) then valid MSFT frame — resync");
        uart_send_byte(8'h13);
        uart_send_byte(8'hAA);   // spurious SOF: starts a doomed frame assembly
        uart_send_byte(8'h37);
        uart_send_byte(8'hDE);
        // finish out the spurious frame so it hits S_EOF and errors out:
        // 0x37 + 0xDE + 19 zeros = 21 bytes after the spurious SOF, so the
        // 21st (a 0x00) lands in S_EOF and fires parse_error -> IDLE
        repeat (19) uart_send_byte(8'h00);
        #(BIT_NS * 3);
        send_frame(8'h01, "MSFT", 32'd4_251_200, 16'd75, 8'h02,
                   64'd1_750_000_004_000_000, 8'h55);
        repeat (10) @(posedge clk100);

        check64("symbol",  {32'd0, dut.symbol}, {32'd0, 32'h4D534654});
        check64("price",   {32'd0, dut.price},  64'd4_251_200);
        check64("qty",     {48'd0, dut.qty},    64'd75);
        check64("side",    {56'd0, dut.side},   64'h02);
        // spurious frame must have raised exactly one more parse_error
        check64("parse_error count", parse_error_seen, 64'd2);
        check64("msg_valid count",   msg_valid_seen,   64'd4);

        //-----------------------------------------------------------------------
        $display("\n==============================================");
        $display("  RESULT: %0d PASS / %0d FAIL", pass_count, fail_count);
        $display("==============================================\n");
        if (fail_count == 0) $display("ALL TESTS PASSED");
        else                 $display("*** FAILURES DETECTED ***");
        $finish;
    end

    // safety timeout
    initial begin
        #50_000_000;   // 50 ms
        $display("*** TIMEOUT — simulation hung ***");
        $finish;
    end

endmodule
