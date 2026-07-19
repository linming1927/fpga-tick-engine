//-----------------------------------------------------------------------------
// tb_vwap_integration.sv — bit-level integration test of the VWAP path
//
// Drives the ACTUAL UART serial waveform into top_arty (real uart_rx, real
// tick_parser, real symcfg / sessctl / vwap_engine / arbiter / frame_tx /
// uart_tx) and watches the FPGA's TX line with a behavioral UART monitor.
// Nothing is reached into except for cross-checks; every stimulus and every
// observed effect crosses the same two wires the host uses.
//
// Tests:
//   V1  Slot 0 config write (TYPE 0x10) acks with a 0x90 echo — baseline
//   V2  TYPE 0x11 session-reset frame echoes as 0x91 (the host's ack path)
//       and does not disturb the SMA/EMA engines
//   V3  Warm-up ticks stream through; every one echoes 0x81; no signal
//       frames appear before WARMUP_N
//   V4  A directed dive-and-bounce fires exactly one 0x85 VWAP signal
//       frame: side=BUY(0x01), price = the evaluated tick's price, and the
//       vwap field matches the bench's independent integer mirror
//   V5  Rise through vwap fires the SELL(0x02) edge, one 0x85 frame
//   V6  A second session-reset clears the engine: the same warm-up stream
//       again produces no signals until re-warmed (state really cleared)
//   V7  SMA/EMA still work end-to-end after all of the above (a crossover
//       still emits its 0x83 frame) — the 3-way arbiter didn't break the
//       existing two strategies
//
// The VWAP warm-up is reduced (parameter override) to keep sim time sane;
// the engine logic is identical at any WARMUP_N (tb_vwap.sv covers the
// engine exhaustively — THIS bench is about the wiring: parser -> sessctl
// -> engine -> arbiter -> FIFO -> frame_tx -> UART, all through real RTL).
//
// Run:  iverilog -g2012 -o simvi.out sim/tb_vwap_integration.sv rtl/*.sv
//       vvp simvi.out
//-----------------------------------------------------------------------------
`timescale 1ns / 1ps

module tb_vwap_integration;

    localparam int CLK_HZ      = 100_000_000;
    localparam int BAUD        = 1_152_000;   // 10x sim speedup, same trick
                                              // as the other benches
    localparam real CLK_PERIOD = 10.0;
    localparam real BIT_NS     = 1e9 / BAUD;

    localparam int VWAP_WARM   = 6;           // small for sim time; logic
                                              // is WARMUP_N-independent
    localparam logic [47:0] SYM = "QQQ   ";

    logic       clk100 = 1'b0;
    logic       ck_rst;
    logic       uart_txd_in = 1'b1;
    logic       uart_rxd_out;
    logic [3:0] led;

    always #(CLK_PERIOD/2.0) clk100 = ~clk100;

    top_arty #(
        .CLK_HZ       ( CLK_HZ    ),
        .BAUD         ( BAUD      ),
        .DEFAULT_SYM0 ( "SPY   "  ),
        .VWAP_WARMUP  ( VWAP_WARM )
    ) dut (
        .clk100       ( clk100       ),
        .ck_rst       ( ck_rst       ),
        .uart_txd_in  ( uart_txd_in  ),
        .uart_rxd_out ( uart_rxd_out ),
        .led          ( led          )
    );

    // ---- scoreboard --------------------------------------------------------
    int pass_count = 0, fail_count = 0;

    task automatic check(input string name, input logic [63:0] got,
                         input logic [63:0] exp);
        if (got === exp) pass_count++;
        else begin
            fail_count++;
            $display("  FAIL  %-44s = 0x%h (expected 0x%h)", name, got, exp);
        end
    endtask

    // ---- UART driver (host -> FPGA), 24-byte v2 frames ---------------------
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
        input logic [63:0] ftstamp
    );
        uart_send_byte(8'hAA);
        uart_send_byte(ftype);
        for (int i = 5; i >= 0; i--) uart_send_byte(fsym[8*i +: 8]);
        for (int i = 3; i >= 0; i--) uart_send_byte(fprice[8*i +: 8]);
        for (int i = 1; i >= 0; i--) uart_send_byte(fqty[8*i +: 8]);
        uart_send_byte(fside);
        for (int i = 7; i >= 0; i--) uart_send_byte(ftstamp[8*i +: 8]);
        uart_send_byte(8'h55);
        #(BIT_NS * 3);
    endtask

    // ---- UART monitor (FPGA -> host), 32-byte frames -----------------------
    logic [7:0] rx_bytes [0:8191];
    int         rx_count = 0;
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
                    rx_bytes[rx_count] = mon_byte;
                    rx_count++;
                end
            end
        end
    end

    function automatic logic [63:0] fld(input int frame, input int offset,
                                        input int nbytes);
        logic [63:0] v;
        v = '0;
        for (int i = 0; i < nbytes; i++)
            v = (v << 8) | rx_bytes[frame*32 + offset + i];
        return v;
    endfunction

    // count / find frames of a given wire type among captured 32-byte frames
    function automatic int count_type(input logic [7:0] wtype);
        int n = 0;
        for (int f = 0; f < rx_count/32; f++)
            if (rx_bytes[f*32 + 1] == wtype) n++;
        return n;
    endfunction

    function automatic int last_of_type(input logic [7:0] wtype);
        int idx = -1;
        for (int f = 0; f < rx_count/32; f++)
            if (rx_bytes[f*32 + 1] == wtype) idx = f;
        return idx;
    endfunction

    // wait until all queued TX frames have fully left the wire. The first
    // version of this task waited for 40 CLOCK cycles of idle line — but a
    // single stop bit holds the line high for ~86 clocks at this baud, so
    // it returned mid-frame and every downstream count raced the UART.
    // Correct form: both FIFOs empty (nothing queued), then the line
    // continuously idle for 20 BIT times (strictly longer than any
    // legitimate stop-bit/inter-byte gap, so nothing is still in flight).
    task automatic drain_tx();
        real idle_ns;
        while (!(dut.fifo_empty && dut.sig_empty)) @(posedge clk100);
        idle_ns = 0;
        while (idle_ns < 20 * BIT_NS) begin
            @(posedge clk100);
            if (uart_rxd_out === 1'b1) idle_ns += CLK_PERIOD;
            else                       idle_ns = 0;
        end
    endtask

    // ---- independent integer mirror of the VWAP math -----------------------
    logic [47:0]  m_sv   = '0;
    logic [71:0]  m_spv  = '0;
    logic [95:0]  m_sppv = '0;

    task automatic m_acc(input logic [31:0] p, input logic [15:0] q);
        m_sv   += 48'(q);
        m_spv  += 72'(48'(p) * 48'(q));
        m_sppv += 96'(80'(64'(p) * 64'(p)) * 80'(q));
    endtask

    function automatic logic [31:0] m_vwap();
        return 32'(96'(m_spv) / 96'(m_sv));
    endfunction

    // send a VWAP-relevant trade tick and fold it into the mirror
    int tick_no = 0;
    task automatic trade(input logic [31:0] p, input logic [15:0] q);
        send_frame(8'h01, SYM, p, q, 8'h01, 64'(1_000_000 + tick_no));
        tick_no++;
        m_acc(p, q);
        // frame time (~250 baud-bits) dwarfs the ~200-cycle evaluation, so
        // every tick is evaluated before the next arrives — the slow-path
        // regime, which is exactly what a live session at this link IS
    endtask

    int sig85_before, f;

    initial begin
        ck_rst = 1'b0;
        repeat (20) @(posedge clk100);
        ck_rst = 1'b1;
        repeat (40) @(posedge clk100);

        //-----------------------------------------------------------------
        $display("[V1] slot 0 config write acks with 0x90");
        //-----------------------------------------------------------------
        send_frame(8'h10, SYM, 32'd0, 16'd0, 8'h01, 64'd0);
        drain_tx();
        check("V1 exactly one 0x90 ack", count_type(8'h90), 1);
        f = last_of_type(8'h90);
        check("V1 ack echoes the symbol", fld(f, 2, 6), 64'(SYM));

        //-----------------------------------------------------------------
        $display("[V2] TYPE 0x11 session reset echoes as 0x91");
        //-----------------------------------------------------------------
        send_frame(8'h11, SYM, 32'd0, 16'd0, 8'hFF, 64'd0);   // broadcast
        drain_tx();
        check("V2 exactly one 0x91 ack", count_type(8'h91), 1);

        //-----------------------------------------------------------------
        $display("[V3] warm-up: ticks echo 0x81, no signal frames yet");
        //-----------------------------------------------------------------
        // ASCENDING tape: each price >= the running vwap, so above stays 1
        // from the priming evaluation onward and no edge can fire — the
        // first version alternated around vwap, which legitimately fires a
        // SELL on the first post-priming up-tick (every alternating tape
        // does: it crosses vwap every tick). Verified tick-by-tick against
        // the integer model: zero events on this tape.
        for (int i = 0; i < VWAP_WARM + 2; i++)
            trade(32'd4_000_000 + 32'(i * 1000), 16'd10);
        drain_tx();
        check("V3 all ticks echoed",
              count_type(8'h81), VWAP_WARM + 2);
        check("V3 no VWAP signals during/right after warm-up",
              count_type(8'h85), 0);
        check("V3 no stray SMA/EMA signals from a flat tape",
              count_type(8'h83) + count_type(8'h84), 0);

        //-----------------------------------------------------------------
        $display("[V4] dive below band, bounce back -> one 0x85 BUY");
        //-----------------------------------------------------------------
        trade(32'd3_940_000, 16'd10);       // dive: below band (below=1)
        trade(32'd3_935_000, 16'd10);       // stay below
        drain_tx();
        sig85_before = count_type(8'h85);
        check("V4 no signal while still below the band",
              sig85_before, 0);
        // recover INSIDE the band but below the dragged-down vwap —
        // exact integer model: the dives pull vwap to 3988909 and inflate
        // sigma to ~25.7k, so 3.975M gives diff ~13.9k < sigma -> the
        // bounce (BUY) edge, on exactly this evaluation
        trade(32'd3_975_000, 16'd10);
        drain_tx();
        check("V4 exactly one VWAP signal frame", count_type(8'h85), 1);
        f = last_of_type(8'h85);
        check("V4 side is BUY",        fld(f, 14, 1), 64'h01);
        check("V4 symbol",             fld(f, 2, 6),  64'(SYM));
        check("V4 price = the evaluated tick's price",
              fld(f, 8, 4), 64'd3_975_000);
        check("V4 vwap field matches the independent mirror",
              fld(f, 15, 4), 64'(m_vwap()));
        check("V4 eval_skips field is 0 at link rate",
              fld(f, 19, 4), 64'd0);

        //-----------------------------------------------------------------
        $display("[V5] rise through vwap -> one 0x85 SELL");
        //-----------------------------------------------------------------
        trade(32'd4_020_000, 16'd10);       // above vwap (3991500 exactly)
        drain_tx();
        check("V5 a second VWAP signal frame", count_type(8'h85), 2);
        f = last_of_type(8'h85);
        check("V5 side is SELL",       fld(f, 14, 1), 64'h02);
        check("V5 vwap field matches mirror again",
              fld(f, 15, 4), 64'(m_vwap()));

        //-----------------------------------------------------------------
        $display("[V6] second session reset really clears the engine");
        //-----------------------------------------------------------------
        send_frame(8'h11, SYM, 32'd0, 16'd0, 8'hFF, 64'd0);
        drain_tx();
        m_sv = '0; m_spv = '0; m_sppv = '0;
        check("V6 0x91 ack for the second reset", count_type(8'h91), 2);
        // the SAME ascending tape: if state truly cleared, this replays
        // the zero-event warm-up (verified against the model); if any
        // stale accumulator or edge state survived the reset, the first
        // ticks would evaluate against corrupt sums and could fire
        for (int i = 0; i < VWAP_WARM + 2; i++)
            trade(32'd4_000_000 + 32'(i * 1000), 16'd10);
        drain_tx();
        check("V6 no new VWAP signals after reset + re-warm-up",
              count_type(8'h85), 2);

        //-----------------------------------------------------------------
        $display("[V7] SMA path still works through the 3-way arbiter");
        //-----------------------------------------------------------------
        // drive a hard trend reversal: FAST_N=8/SLOW_N=32 need real data.
        // descending tape then a hard spike gives the fast SMA a clean
        // upward crossover through the slow — the same recipe
        // tb_indicator_e2e uses.
        for (int i = 0; i < 40; i++)
            trade(32'd4_000_000 - 32'(i * 3000), 16'd1);
        for (int i = 0; i < 12; i++)
            trade(32'd4_100_000 + 32'(i * 8000), 16'd1);
        drain_tx();
        check("V7 an SMA signal frame arrived (0x83)",
              (count_type(8'h83) > 0), 1);
        check("V7 no parse errors across the whole bench",
              dut.parse_error_count, 0);
        check("V7 nothing was dropped from the signal FIFO",
              dut.sig_drop_count, 0);
        check("V7 nothing was dropped from the pend queue",
              dut.pend_drop_count, 0);

        $display("==============================================");
        $display("  RESULT: %0d PASS / %0d FAIL", pass_count, fail_count);
        $display("==============================================");
        if (fail_count == 0) $display("ALL TESTS PASSED");
        $finish;
    end

    initial begin
        #400_000_000;   // 400 ms sim time
        $display("TIMEOUT — bench hung (captured %0d bytes)", rx_count);
        $finish;
    end

endmodule
