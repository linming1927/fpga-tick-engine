//-----------------------------------------------------------------------------
// tb_vwap.sv — unit test of vwap_engine against a bit-exact mirror model
//
// Same methodology as tb_ema.sv / tb_indicator.sv: the bench maintains an
// independent behavioral mirror (session sums, truncating integer divides,
// squared-domain band test, edge/priming rules, SELL-dominates convention)
// and compares vwap_out, event count, and event side against the DUT.
//
// The mirror deliberately implements the ENGINE's event convention
// (position-independent edges — see vwap_engine.sv header), NOT the host
// scorecard's position-gated on_tick(): those two streams differ by
// design, and the host mirror model will adopt the engine convention when
// the FPGA path goes live.
//
// Phases:
//   P1  Warm-up: no events during WARMUP_N ticks; vwap_out exact per eval
//   P2  Directed bounce: dive below band, recover -> exactly one BUY;
//       then rise through vwap -> exactly one SELL
//   P3  Filtering: wrong symbol / quote frames leave all state untouched
//   P4  Session reset: sess_rst clears (vwap_valid drops, count restarts,
//       priming re-required); state_rst behaves identically
//   P5  400-tick pseudo-random walk, evaluated tick-by-tick (bench waits
//       for each evaluation), vwap + every event checked against mirror
//   P6  Burst: 40 back-to-back ticks (1/cycle) — accumulators must be
//       EXACT afterward (checked via the next evaluation's vwap against
//       the mirror's all-tick sums), eval_skips must be nonzero, and the
//       engine must evaluate normally again afterward
//   P7  Gap-through-both-edges: from below-band straight to above vwap in
//       one evaluated tick -> exactly one event, and it is SELL
//   P8  qty=0 ticks only -> sum_v stays 0, no divide, no event, no hang
//
// Run:  iverilog -g2012 -o simvwap.out sim/tb_vwap.sv rtl/vwap_engine.sv
//-----------------------------------------------------------------------------
`timescale 1ns / 1ps

module tb_vwap;

    localparam int WARMUP_N = 8;
    localparam int K2_Q8    = 256;               // k = 1.0
    localparam int V_W      = 48;
    localparam int PV_W     = 72;
    localparam int PPV_W    = 96;
    localparam logic [47:0] SYM = "TEST  ";

    logic clk = 1'b0;
    logic rst_n;
    always #5 clk = ~clk;

    logic        tick_valid = 1'b0;
    logic [7:0]  msg_type   = 8'h01;
    logic [47:0] symbol     = SYM;
    logic [31:0] price      = '0;
    logic [15:0] qty        = '0;
    logic [63:0] host_ts    = '0, fpga_ts = '0;
    logic [47:0] target_symbol = SYM;
    logic        slot_en    = 1'b1;
    logic        state_rst  = 1'b0;
    logic        sess_rst   = 1'b0;

    logic        signal_valid;
    logic [47:0] signal_symbol;
    logic [7:0]  signal_side;
    logic [31:0] signal_price;
    logic [63:0] signal_host_ts, signal_fpga_ts;
    logic [31:0] vwap_out;
    logic        vwap_valid;
    logic [31:0] eval_skips;

    vwap_engine #(
        .WARMUP_N(WARMUP_N), .K2_Q8(K2_Q8),
        .V_W(V_W), .PV_W(PV_W), .PPV_W(PPV_W)
    ) dut (.*);

    int pass_count = 0, fail_count = 0;

    task automatic check(input string name, input logic [63:0] got,
                         input logic [63:0] exp);
        if (got === exp) pass_count++;
        else begin
            fail_count++;
            $display("  FAIL  %-40s = %0d (expected %0d)", name, got, exp);
        end
    endtask

    // ---- DUT event capture --------------------------------------------------
    int         sig_count = 0;
    logic [7:0] last_side;
    always @(posedge clk)
        if (signal_valid) begin
            sig_count <= sig_count + 1;
            last_side <= signal_side;
        end

    // ---- mirror model -------------------------------------------------------
    // Bit-exact integer semantics: unsigned vector division in SV truncates
    // exactly like the DUT's restoring divider (both compute floor for
    // unsigned operands).
    logic [V_W-1:0]   m_sv  = '0;           // Σ volume
    logic [PV_W-1:0]  m_spv = '0;           // Σ p·v
    logic [PPV_W-1:0] m_sppv = '0;          // Σ p²·v
    int               m_ticks = 0;
    bit               m_below = 0, m_above = 0, m_primed = 0;
    int               m_sigs = 0;
    logic [7:0]       m_side = 0;
    logic [31:0]      m_vwap = '0;

    // fold one accepted tick into the mirror sums (no evaluation)
    task automatic m_accum(input logic [31:0] p, input logic [15:0] q);
        m_sv   += V_W'(q);
        m_spv  += PV_W'(48'(p) * 48'(q));
        m_sppv += PPV_W'(80'(64'(p) * 64'(p)) * 80'(q));
        m_ticks += 1;
    endtask

    // evaluate the mirror at the current sums for evaluated-tick price p —
    // mirrors EV_DECIDE exactly, including the SELL-dominates convention
    task automatic m_eval(input logic [31:0] p);
        logic [PPV_W-1:0] q1w, q2w;
        logic [31:0] vwap;
        logic [63:0] msq, vsq, variance, diffsq, thr;
        logic [31:0] diff;
        bit below_now, above_now;
        if (m_sv != '0) begin
        q1w  = PPV_W'(m_spv) / PPV_W'(m_sv);
        q2w  = m_sppv / PPV_W'(m_sv);
        vwap = 32'(q1w);
        msq  = 64'(q2w);
        m_vwap = vwap;
        vsq  = 64'(vwap) * 64'(vwap);
        variance = (msq >= vsq) ? (msq - vsq) : 64'd0;
        diff   = (p < vwap) ? (vwap - p) : 32'd0;
        diffsq = 64'(diff) * 64'(diff);
        thr    = 64'((72'(variance) * 72'(K2_Q8)) >> 8);
        below_now = (p < vwap) && (diffsq > thr);
        above_now = (p >= vwap);
        if (m_ticks >= WARMUP_N) begin
            if (m_primed) begin
                if (!m_above && above_now) begin
                    m_sigs += 1; m_side = 8'h02;
                end else if (m_below && !below_now) begin
                    m_sigs += 1; m_side = 8'h01;
                end
            end
            m_below  = below_now;
            m_above  = above_now;
            m_primed = 1;
        end
        end
    endtask

    task automatic m_clear();
        m_sv = '0; m_spv = '0; m_sppv = '0; m_ticks = 0;
        m_below = 0; m_above = 0; m_primed = 0;
        m_vwap = '0;
    endtask

    // ---- drivers ------------------------------------------------------------
    // one tick, then wait long enough for the full evaluation to complete
    // (3 pipeline + 2*DIV_N + ~6 bookkeeping; generous margin)
    localparam int EVAL_WAIT = 2*PPV_W + 24;

    task automatic tick_slow(input logic [31:0] p, input logic [15:0] q);
        @(negedge clk);
        tick_valid <= 1'b1; msg_type <= 8'h01; symbol <= SYM;
        price <= p; qty <= q;
        @(negedge clk);
        tick_valid <= 1'b0;
        m_accum(p, q);
        repeat (EVAL_WAIT) @(posedge clk);
        m_eval(p);
    endtask

    // a tick that must be IGNORED (wrong symbol or quote type)
    task automatic tick_ignored(input logic [7:0] mt,
                                input logic [47:0] sym_in,
                                input logic [31:0] p,
                                input logic [15:0] q);
        @(negedge clk);
        tick_valid <= 1'b1; msg_type <= mt; symbol <= sym_in;
        price <= p; qty <= q;
        @(negedge clk);
        tick_valid <= 1'b0; msg_type <= 8'h01; symbol <= SYM;
        repeat (8) @(posedge clk);
    endtask

    // back-to-back burst: one accepted tick per cycle, no waiting
    task automatic tick_burst(input logic [31:0] p, input logic [15:0] q);
        tick_valid <= 1'b1; msg_type <= 8'h01; symbol <= SYM;
        price <= p; qty <= q;
        @(negedge clk);
        m_accum(p, q);
    endtask

    // ---- pseudo-random (deterministic LCG, same style as tb_ema) ------------
    int unsigned lcg = 32'hC0FFEE01;
    function automatic int unsigned rnd();
        lcg = lcg * 32'd1664525 + 32'd1013904223;
        return lcg;
    endfunction

    logic [31:0] p_walk;
    int          exp_sigs_snapshot;

    initial begin
        rst_n = 1'b0;
        repeat (4) @(posedge clk);
        rst_n = 1'b1;
        repeat (2) @(posedge clk);

        //-----------------------------------------------------------------
        $display("[P1] warm-up: no events, exact vwap per evaluation");
        //-----------------------------------------------------------------
        for (int i = 0; i < WARMUP_N; i++) begin
            tick_slow(32'd4_000_000 + 32'(i * 100), 16'd10);
            check($sformatf("P1 vwap after tick %0d", i),
                  vwap_out, m_vwap);
        end
        check("P1 no events during warm-up", sig_count, 0);
        check("P1 vwap_valid after warm-up", vwap_valid, 1);
        check("P1 no skips at slow rate", eval_skips, 0);

        //-----------------------------------------------------------------
        $display("[P2] directed bounce: BUY on band re-entry, SELL at vwap");
        //-----------------------------------------------------------------
        // establish spread so the band has real width, priming happens on
        // the first warm evaluation (already primed by P1's last ticks)
        tick_slow(32'd4_002_000, 16'd10);
        tick_slow(32'd3_998_000, 16'd10);
        // dive well below the lower band...
        tick_slow(32'd3_960_000, 16'd10);
        tick_slow(32'd3_950_000, 16'd10);
        exp_sigs_snapshot = m_sigs;
        // ...recover to 3.985M — computed exactly against the integer
        // model: the dive drags vwap to ~3.9921M and inflates sigma to
        // ~16.6k, so 3.985M is back INSIDE the band (diff 7.1k < sigma)
        // yet still BELOW vwap -> bounce = BUY. (First attempt used
        // 3.995M, which lands ABOVE the dragged vwap -> SELL edge — the
        // band-feedback subtlety this comment exists to remember.)
        tick_slow(32'd3_985_000, 16'd10);
        check("P2 event totals after bounce", sig_count, m_sigs);
        check("P2 a BUY fired on the bounce",
              (m_sigs > exp_sigs_snapshot) && (m_side == 8'h01), 1);
        check("P2 sides agree", last_side, m_side);
        // rise through vwap: revert = SELL
        tick_slow(32'd4_010_000, 16'd10);
        check("P2 event totals after revert", sig_count, m_sigs);
        check("P2 a SELL fired at vwap", m_side, 8'h02);
        check("P2 sides agree (sell)", last_side, m_side);

        //-----------------------------------------------------------------
        $display("[P3] filtering: wrong symbol / quotes leave state alone");
        //-----------------------------------------------------------------
        exp_sigs_snapshot = sig_count;
        tick_ignored(8'h01, "OTHER ", 32'd1_000_000, 16'd999);
        tick_ignored(8'h02, SYM,       32'd1_000_000, 16'd999); // quote
        tick_slow(32'd4_000_000, 16'd10);   // a real tick evaluates fine
        check("P3 vwap unaffected by ignored frames", vwap_out, m_vwap);
        check("P3 events unaffected", sig_count, m_sigs);

        //-----------------------------------------------------------------
        $display("[P4] session reset: sess_rst and state_rst both clear");
        //-----------------------------------------------------------------
        @(negedge clk); sess_rst <= 1'b1;
        @(negedge clk); sess_rst <= 1'b0;
        m_clear();
        repeat (4) @(posedge clk);
        check("P4 vwap_valid drops on sess_rst", vwap_valid, 0);
        check("P4 vwap_out cleared", vwap_out, 0);
        // rebuild past warm-up; priming must be re-required (first warm
        // evaluation cannot fire even across a band edge)
        for (int i = 0; i < WARMUP_N + 2; i++)
            tick_slow(32'd5_000_000 + 32'(rnd() % 4000), 16'd5);
        check("P4 vwap_valid after re-warm-up", vwap_valid, 1);
        check("P4 vwap exact after re-warm-up", vwap_out, m_vwap);
        check("P4 event totals still agree", sig_count, m_sigs);
        // state_rst same behavior
        @(negedge clk); state_rst <= 1'b1;
        @(negedge clk); state_rst <= 1'b0;
        m_clear();
        repeat (4) @(posedge clk);
        check("P4 vwap_valid drops on state_rst", vwap_valid, 0);
        for (int i = 0; i < WARMUP_N + 2; i++)
            tick_slow(32'd4_000_000 + 32'(rnd() % 4000), 16'd5);
        check("P4 exact after state_rst rebuild", vwap_out, m_vwap);

        //-----------------------------------------------------------------
        $display("[P5] 400-tick random walk, every evaluation checked");
        //-----------------------------------------------------------------
        p_walk = 32'd4_000_000;
        for (int i = 0; i < 400; i++) begin
            // ±0.15%-ish steps around the walk, qty 1..64
            if (rnd() & 1) p_walk = p_walk + 32'(rnd() % 6000);
            else           p_walk = p_walk - 32'(rnd() % 6000);
            tick_slow(p_walk, 16'(1 + (rnd() % 64)));
            check($sformatf("P5 vwap tick %0d", i), vwap_out, m_vwap);
            check($sformatf("P5 sigs tick %0d", i), sig_count, m_sigs);
            if (sig_count > 0)
                check($sformatf("P5 side tick %0d", i), last_side, m_side);
        end
        check("P5 still zero skips at slow rate", eval_skips, 0);

        //-----------------------------------------------------------------
        $display("[P6] burst: 40 back-to-back ticks — exact sums, skips>0");
        //-----------------------------------------------------------------
        @(negedge clk);
        for (int i = 0; i < 40; i++)
            tick_burst(32'd4_000_000 + 32'(rnd() % 2000),
                       16'(1 + (rnd() % 8)));
        tick_valid <= 1'b0;
        // let the in-flight evaluation(s) drain: the LAST coalesced
        // evaluation is guaranteed to snapshot the all-40-tick sums
        // (the burst absorbs in ~43 cycles, far shorter than one
        // evaluation, so by the time the final pending tick starts its
        // divide, every tick is in the accumulators)
        repeat (3*EVAL_WAIT) @(posedge clk);
        check("P6 eval_skips nonzero under burst",
              (eval_skips > 0), 1);
        // exactness check on the sums themselves: the DUT's final
        // evaluation divided the all-40 sums; the mirror holds the same
        // sums — this is the "accumulators never corrupt under burst"
        // guarantee, end to end, with no event comparison involved
        // (coalesced edge decisions aren't individually comparable by
        // design; P7 resyncs edge state deterministically via sess_rst)
        check("P6 vwap exact after burst",
              vwap_out, 32'(PPV_W'(m_spv) / PPV_W'(m_sv)));

        //-----------------------------------------------------------------
        $display("[P7] gap through band AND vwap in one tick -> one SELL");
        //-----------------------------------------------------------------
        @(negedge clk); sess_rst <= 1'b1;
        @(negedge clk); sess_rst <= 1'b0;
        m_clear();
        // both edge states are now identically cleared; realign the
        // counters the burst may have skewed (the DUT can fire during
        // coalesced evaluations the mirror never ran — by design)
        m_sigs = sig_count;
        m_side = last_side;
        repeat (4) @(posedge clk);
        // warm with spread around 4.000M so the band has width
        for (int i = 0; i < WARMUP_N + 2; i++)
            tick_slow((i & 1) ? 32'd4_004_000 : 32'd3_996_000, 16'd10);
        // dive far below band (below=1, above=0)...
        tick_slow(32'd3_940_000, 16'd10);
        tick_slow(32'd3_935_000, 16'd10);
        exp_sigs_snapshot = sig_count;
        // ...gap straight above vwap: both edges in one evaluation
        tick_slow(32'd4_050_000, 16'd10);
        check("P7 exactly one event fired",
              sig_count, exp_sigs_snapshot + 1);
        check("P7 and it is the SELL (dominant edge)", last_side, 8'h02);
        check("P7 mirror agrees", m_side, 8'h02);
        check("P7 totals agree", sig_count, m_sigs);

        //-----------------------------------------------------------------
        $display("[P8] qty=0 ticks only: no divide, no event, no hang");
        //-----------------------------------------------------------------
        @(negedge clk); sess_rst <= 1'b1;
        @(negedge clk); sess_rst <= 1'b0;
        m_clear();
        repeat (4) @(posedge clk);
        for (int i = 0; i < 5; i++)
            tick_slow(32'd4_000_000, 16'd0);   // wire allows qty=0
        check("P8 no events from zero-volume session", sig_count, m_sigs);
        check("P8 vwap_out still cleared", vwap_out, 0);
        // and a real tick afterward evaluates normally
        tick_slow(32'd4_000_000, 16'd10);
        check("P8 recovers with the first real volume", vwap_out, m_vwap);

        $display("==============================================");
        $display("  RESULT: %0d PASS / %0d FAIL", pass_count, fail_count);
        $display("==============================================");
        if (fail_count == 0) $display("ALL TESTS PASSED");
        $finish;
    end

    initial begin
        #80_000_000;
        $display("TIMEOUT — bench hung");
        $finish;
    end

endmodule
