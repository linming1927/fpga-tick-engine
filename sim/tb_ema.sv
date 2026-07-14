//-----------------------------------------------------------------------------
// tb_ema.sv — unit test of ema_engine against a mirror model
//
// Same methodology as tb_indicator.sv: the bench maintains an independent
// behavioral mirror (accumulators, seed, arithmetic-shift read-out, warmup,
// priming, strict crossover) and compares ema_fast, ema_slow, signal count
// and side against the DUT after EVERY tick.
//
// Phases:
//   P1  Seed + warm-up (8 descending trades): exact accumulator lineage,
//       no signal
//   P2  Directed crossovers: spike -> BUY, slump -> SELL
//   P3  Filtering: wrong symbol / quotes leave state untouched
//   P4  Convergence: constant price for 60 ticks — the extended-precision
//       accumulator must converge to EXACTLY the price (this is the test
//       a truncated-integer EMA fails: it stalls 2^K-1 short)
//   P5  300-tick pseudo-random walk, every tick checked
//
// Run:  iverilog -g2012 -o simema.out sim/tb_ema.sv rtl/ema_engine.sv
//-----------------------------------------------------------------------------
`timescale 1ns / 1ps

module tb_ema;

    localparam int K_FAST   = 1;
    localparam int K_SLOW   = 3;
    localparam int WARMUP_N = 8;
    localparam logic [47:0] SYM = "TEST  ";

    logic clk = 1'b0;
    logic rst_n;
    always #5 clk = ~clk;

    logic        tick_valid = 1'b0;
    logic [7:0]  msg_type;
    logic [47:0] symbol;
    logic [31:0] price;
    logic [47:0] target_symbol = SYM;     // runtime slot (v2)
    logic        slot_en = 1'b1;
    logic        state_rst = 1'b0;
    logic [47:0] signal_symbol;
    logic [63:0] host_ts = 0, fpga_ts = 0;
    logic        signal_valid;
    logic [7:0]  signal_side;
    logic [31:0] signal_price;
    logic [63:0] signal_host_ts, signal_fpga_ts;
    logic [31:0] ema_fast, ema_slow;
    logic        emas_valid;

    ema_engine #(
        .K_FAST(K_FAST), .K_SLOW(K_SLOW),
        .WARMUP_N(WARMUP_N)
    ) dut (.*);

    int pass_count = 0, fail_count = 0;

    task automatic check(input string name, input logic [63:0] got,
                         input logic [63:0] exp);
        if (got === exp) pass_count++;
        else begin
            fail_count++;
            $display("  FAIL  %-28s = %0d (expected %0d)", name, got, exp);
        end
    endtask

    int         sig_count = 0;
    logic [7:0] last_side;
    always @(posedge clk)
        if (signal_valid) begin
            sig_count <= sig_count + 1;
            last_side <= signal_side;
        end

    // ---- mirror model ---------------------------------------------------------
    logic [63:0] m_af = 0, m_as = 0;         // extended accumulators
    bit          m_seeded = 0, m_primed = 0, m_above = 0;
    int          m_warm = 0, m_sigs = 0;
    logic [7:0]  m_side = 0;
    logic [31:0] m_ef, m_es;

    task automatic model_tick(input logic [31:0] p);
        if (!m_seeded) begin
            m_af = 64'(p) << K_FAST;
            m_as = 64'(p) << K_SLOW;
            m_seeded = 1;
        end else begin
            m_af = m_af + p - (m_af >> K_FAST);
            m_as = m_as + p - (m_as >> K_SLOW);
        end
        if (m_warm < WARMUP_N) m_warm++;
        m_ef = 32'(m_af >> K_FAST);
        m_es = 32'(m_as >> K_SLOW);
        if (m_warm == WARMUP_N) begin
            if (m_primed && ((m_ef > m_es) != m_above)) begin
                m_sigs++;
                m_side = (m_ef > m_es) ? 8'h01 : 8'h02;
            end
            m_above  = (m_ef > m_es);
            m_primed = 1;
        end
    endtask

    task automatic dut_tick(input logic [7:0] t, input logic [47:0] s,
                            input logic [31:0] p);
        @(negedge clk);
        msg_type = t; symbol = s; price = p;
        host_ts = host_ts + 1; fpga_ts = fpga_ts + 100;
        tick_valid = 1'b1;
        @(negedge clk);
        tick_valid = 1'b0;
        repeat (6) @(negedge clk);
    endtask

    task automatic trade(input logic [31:0] p);
        dut_tick(8'h01, SYM, p);
        model_tick(p);
        check("ema_fast", {32'd0, ema_fast}, {32'd0, m_ef});
        check("ema_slow", {32'd0, ema_slow}, {32'd0, m_es});
        check("sig_count", sig_count, m_sigs);
        if (m_sigs > 0) check("side", {56'd0, last_side}, {56'd0, m_side});
    endtask

    logic [31:0] lcg = 32'd42;
    function automatic logic [31:0] rnd();
        lcg = lcg * 32'd1103515245 + 32'd12345;
        return lcg;
    endfunction

    logic [31:0] walk;
    int start_sigs;

    initial begin
        $dumpfile("tb_ema.vcd");
        $dumpvars(0, tb_ema);
        rst_n = 1'b0; repeat (5) @(negedge clk);
        rst_n = 1'b1; repeat (5) @(negedge clk);

        $display("\n[P1] seed + warm-up: 8 descending trades");
        trade(32'd2000); trade(32'd1900); trade(32'd1800); trade(32'd1700);
        trade(32'd1600); trade(32'd1500); trade(32'd1400); trade(32'd1300);
        check("no warm-up signals", sig_count, 0);
        check("warmed up", {63'd0, emas_valid}, 64'd1);
        // hand anchors (also used by tb_indicator_e2e and the host tests)
        check("fast anchor 1399", {32'd0, ema_fast}, 64'd1399);
        check("slow anchor 1725", {32'd0, ema_slow}, 64'd1725);

        $display("\n[P2] directed crossovers");
        trade(32'd3000);
        check("golden cross", sig_count, 1);
        check("BUY", {56'd0, last_side}, 64'h01);
        check("fast anchor 2200", {32'd0, ema_fast}, 64'd2200);
        check("slow anchor 1884", {32'd0, ema_slow}, 64'd1884);
        trade(32'd3100);
        check("no retrigger", sig_count, 1);
        trade(32'd100); trade(32'd100); trade(32'd100);
        if (m_sigs == 2) check("death cross SELL", {56'd0, last_side}, 64'h02);

        $display("\n[P3] filtering");
        dut_tick(8'h01, "XXXX  ", 32'd9_999_999);
        dut_tick(8'h02, SYM,    32'd9_999_999);
        trade(32'd100);

        $display("\n[P4] convergence: constant price, no deadband stall");
        repeat (160) trade(32'd1_234_567);   // K=3 needs ~105 ticks from 4 decades away
        check("fast converged exactly", {32'd0, ema_fast}, 64'd1_234_567);
        check("slow converged exactly", {32'd0, ema_slow}, 64'd1_234_567);

        $display("\n[P5] 300-tick pseudo-random walk");
        start_sigs = m_sigs;
        walk = 32'd1_500_000;
        for (int k = 0; k < 300; k++) begin
            if ((rnd() >> 16) & 1) walk = walk + (rnd() % 32'd60_000);
            else                   walk = walk - (rnd() % 32'd60_000);
            if (walk < 32'd100_000 || walk > 32'd100_000_000)
                walk = 32'd1_500_000;
            trade(walk);
        end
        $display("       walk produced %0d crossovers, all matched",
                 m_sigs - start_sigs);

        $display("\n==============================================");
        $display("  RESULT: %0d PASS / %0d FAIL", pass_count, fail_count);
        $display("==============================================\n");
        if (fail_count == 0) $display("ALL TESTS PASSED");
        $finish;
    end

    initial begin
        #20_000_000;
        $display("*** TIMEOUT ***");
        $finish;
    end

endmodule
