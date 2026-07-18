//-----------------------------------------------------------------------------
// vwap_engine.sv — session-VWAP mean-reversion bounce detector
//
// The hardware sibling of host/vwap_bounce_strategy.py — the strategy the
// multi-year QQQ/VTI backtests found consistently profitable, built in
// fabric only AFTER the host-side version earned it (same order of
// operations as every engine here: host model first, RTL second, and the
// two verified against each other before anything is trusted).
//
// ---- What it computes -------------------------------------------------------
// Per accepted trade tick, three session accumulators:
//     sum_v   += qty                       (Σ volume)
//     sum_pv  += price * qty               (Σ price·volume)
//     sum_ppv += price * price * qty       (Σ price²·volume)
// Then, per EVALUATION (see coalescing below):
//     vwap     = sum_pv  / sum_v           (truncating integer divide)
//     mean_sq  = sum_ppv / sum_v
//     variance = max(0, mean_sq - vwap²)   (clamped: truncation can push
//                                           the subtraction slightly < 0)
// Band test WITHOUT a square root — the comparison the strategy actually
// needs never requires stdev itself, only "is price below vwap - k·stdev",
// which is equivalent in the squared domain:
//     below_band  ⟺  (price < vwap)  &&  (vwap-price)² > (K2_Q8·variance)»8
// K2_Q8 is k² in Q8 fixed point (256 = k of 1.0, the host default).
// No sqrt core, no CORDIC — one extra multiply.
//
// ---- Events, not positions --------------------------------------------------
// The host model gates signal GENERATION on its own position (buy checks
// only while flat, sell checks only while holding). Hardware cannot know
// host position — fills happen upstream. So this engine emits POSITION-
// INDEPENDENT EDGE EVENTS and the host layer applies position logic:
//     BUY  (0x01): below_band was 1, now 0 — price bounced back up
//                  through the lower band (the mean-reversion entry)
//     SELL (0x02): price crossed from below vwap to >= vwap — reverted
//                  to fair value (the take-profit edge)
// If one evaluation sees BOTH edges (a gap from below-band to above-vwap),
// SELL wins and exactly one event fires — the dominant edge; convention
// fixed here so the host mirror can match it bit-for-bit.
// CONSEQUENCE, stated plainly: this event stream is NOT identical to the
// host-only VWAPBounceScorecard.on_tick() signal stream (which never even
// generates a sell while flat). When the FPGA path goes live, the host
// mirror model must implement THIS convention; scored totals of the two
// paths differ by construction, not by bug.
//
// ---- Session boundary -------------------------------------------------------
// VWAP is only meaningful within one trading session. Two reset inputs,
// ORed: state_rst (this slot was rewritten — symcfg's existing pulse, same
// as SMA/EMA) and sess_rst (NEW: host-commanded session reset, decoded
// upstream from a TYPE 0x11 control frame). The host knows the market
// calendar; the fabric does not — deliberately. Building ET-calendar/DST
// logic in hardware would be new machinery with real bug surface; a
// host-sent "new session" pulse reuses the exact
// write-a-frame / echo-is-the-ack verification path symcfg proved out.
// Either reset clears accumulators, warm-up, edge state, and ABORTS any
// in-flight evaluation (its snapshot spans the boundary — invalid).
//
// ---- High-volume design: coalescing, never corruption -----------------------
// Built for tick rates far beyond the current link on purpose (the paid
// data feed / faster link future). Two independent planes:
//   * ACCUMULATION accepts a tick EVERY cycle indefinitely — a 3-stage
//     multiply/add pipeline (A1 products, A2 second product, A3 the three
//     accumulator adds, together, so the sums are mutually consistent at
//     any snapshot instant). Back-to-back ticks stream through; the sums
//     are always exact regardless of rate. Correctness never degrades.
//   * EVALUATION (2 serial divides + bookkeeping ≈ 2·DIV_N+4 cycles) runs
//     from a SNAPSHOT of the sums. If ticks land while a divide is in
//     flight, the newest is held PENDING; when the divide completes, the
//     next evaluation starts immediately from the LATEST sums.
//     Intermediate ticks between snapshots aren't individually evaluated —
//     they are COALESCED, and eval_skips counts every coalesced tick so
//     the host can SEE saturation instead of guessing.
// Budget honesty (100 MHz, default widths): one evaluation ≈ 196 cycles
// ≈ 2 µs ≈ ~500 000 evaluations/sec. The CURRENT link (24-byte frames at
// 115 200 baud) tops out near 480 ticks/sec — three orders of magnitude
// of headroom, so per-tick evaluation holds through any realistic link
// upgrade, and past that the failure mode is documented decimation of the
// signal-check rate with exact accumulators — never wrong numbers.
//
// ---- Width budget (parameterized; defaults sized with real margin) ----------
//   price 32b (price_e4; $1677 needs 24b — 32b is the bus width)
//   qty   16b (frame field width)
//   sum_v   V_W  = 48b : 2.8e14 shares/session of headroom
//   sum_pv  PV_W = 72b : p·q ≤ 2^48/tick; 2^24 such ticks before overflow
//   sum_ppv PPV_W= 96b : realistic (24b price) p²·q ≤ 2^64/tick, 2^32
//                        ticks of headroom
// Extreme-price overflow beyond these widths is documented, not defended —
// the same convention the project uses elsewhere (see symcfg's duplicate-
// slot note). The widths are parameters; grow them if you ever trade
// six-figure prices at index-level volume.
//
// ---- Warm-up and priming ----------------------------------------------------
// The band is noise until the session has real data: events are gated
// until WARMUP_N accepted ticks (host default min_session_ticks=20; the
// parameter default matches). The first completed evaluation after warm-up
// PRIMES the edge state without firing — same rule, same reason, as the
// SMA and EMA engines (see indicator_engine.sv header).
//-----------------------------------------------------------------------------
`timescale 1ns / 1ps
`default_nettype none

module vwap_engine #(
    parameter int          WARMUP_N = 20,    // ticks before events allowed
    parameter int          K2_Q8    = 256,   // k² in Q8 (256 = k of 1.0)
    parameter int          V_W      = 48,    // Σ volume width
    parameter int          PV_W     = 72,    // Σ price·volume width
    parameter int          PPV_W    = 96     // Σ price²·volume width
)(
    input  wire  logic        clk,
    input  wire  logic        rst_n,

    // runtime symbol slot (symcfg register file — same as SMA/EMA)
    input  wire  logic [47:0] target_symbol,
    input  wire  logic        slot_en,
    input  wire  logic        state_rst,     // 1-cycle: slot rewritten
    input  wire  logic        sess_rst,      // 1-cycle: host-commanded new
                                             // session (TYPE 0x11 upstream)

    // decoded tick bus
    input  wire  logic        tick_valid,
    input  wire  logic [7:0]  msg_type,
    input  wire  logic [47:0] symbol,
    input  wire  logic [31:0] price,
    input  wire  logic [15:0] qty,
    input  wire  logic [63:0] host_ts,
    input  wire  logic [63:0] fpga_ts,

    // signal out (one-cycle pulse, fields valid during the pulse)
    output logic        signal_valid,
    output logic [47:0] signal_symbol,
    output logic [7:0]  signal_side,      // 0x01 bounce-buy / 0x02 revert-sell
    output logic [31:0] signal_price,     // price of the evaluated tick
    output logic [63:0] signal_host_ts,
    output logic [63:0] signal_fpga_ts,

    // status / debug
    output logic [31:0] vwap_out,         // latest completed evaluation
    output logic        vwap_valid,       // session is warmed up
    output logic [31:0] eval_skips        // coalesced (unevaluated) ticks —
                                          // nonzero means the tick rate
                                          // exceeded the eval rate; the
                                          // sums stay exact regardless
);

    localparam logic [7:0] TYPE_TRADE = 8'h01;
    localparam logic [7:0] SIDE_BUY   = 8'h01;
    localparam logic [7:0] SIDE_SELL  = 8'h02;

    localparam int DIV_N = PPV_W;          // widest dividend → shared width
    localparam int CW    = $clog2(DIV_N);  // divide-step counter width

    logic accept;
    assign accept = tick_valid
                 && slot_en
                 && (symbol   == target_symbol)
                 && (msg_type == TYPE_TRADE);

    logic clear;                           // either reset flavor — header
    assign clear = state_rst | sess_rst;

    //-------------------------------------------------------------------------
    // Accumulation plane — a tick every cycle, forever.
    //-------------------------------------------------------------------------
    logic        a1_v;
    logic [47:0] a1_pq;
    logic [63:0] a1_pp;
    logic [15:0] a1_qty;
    logic [31:0] a1_price;
    logic [47:0] a1_sym;
    logic [63:0] a1_hts, a1_fts;

    logic        a2_v;
    logic [79:0] a2_ppq;
    logic [47:0] a2_pq;
    logic [15:0] a2_qty;
    logic [31:0] a2_price;
    logic [47:0] a2_sym;
    logic [63:0] a2_hts, a2_fts;

    logic [V_W-1:0]   sum_v;
    logic [PV_W-1:0]  sum_pv;
    logic [PPV_W-1:0] sum_ppv;
    logic [31:0]      sess_ticks;

    logic        a3_done;                  // pulses when sums absorbed a tick
    logic [31:0] a3_price;
    logic [47:0] a3_sym;
    logic [63:0] a3_hts, a3_fts;

    always_ff @(posedge clk) begin
        if (!rst_n || clear) begin
            a1_v <= 1'b0; a2_v <= 1'b0; a3_done <= 1'b0;
            a1_pq <= '0; a1_pp <= '0; a1_qty <= '0; a1_price <= '0;
            a1_sym <= '0; a1_hts <= '0; a1_fts <= '0;
            a2_ppq <= '0; a2_pq <= '0; a2_qty <= '0; a2_price <= '0;
            a2_sym <= '0; a2_hts <= '0; a2_fts <= '0;
            a3_price <= '0; a3_sym <= '0; a3_hts <= '0; a3_fts <= '0;
            sum_v <= '0; sum_pv <= '0; sum_ppv <= '0;
            sess_ticks <= '0;
        end else begin
            a1_v <= accept;
            if (accept) begin
                a1_pq    <= 48'(price) * 48'(qty);
                a1_pp    <= 64'(price) * 64'(price);
                a1_qty   <= qty;
                a1_price <= price;
                a1_sym   <= symbol;
                a1_hts   <= host_ts;
                a1_fts   <= fpga_ts;
            end
            a2_v <= a1_v;
            if (a1_v) begin
                a2_ppq   <= 80'(a1_pp) * 80'(a1_qty);
                a2_pq    <= a1_pq;
                a2_qty   <= a1_qty;
                a2_price <= a1_price;
                a2_sym   <= a1_sym;
                a2_hts   <= a1_hts;
                a2_fts   <= a1_fts;
            end
            a3_done <= a2_v;
            if (a2_v) begin
                sum_v      <= sum_v   + V_W'(a2_qty);
                sum_pv     <= sum_pv  + PV_W'(a2_pq);
                sum_ppv    <= sum_ppv + PPV_W'(a2_ppq);
                sess_ticks <= sess_ticks + 1;
                a3_price   <= a2_price;
                a3_sym     <= a2_sym;
                a3_hts     <= a2_hts;
                a3_fts     <= a2_fts;
            end
        end
    end

    assign vwap_valid = (sess_ticks >= WARMUP_N);

    //-------------------------------------------------------------------------
    // Evaluation plane — snapshot, two serial divides, one decision.
    //
    //   EV_IDLE   : wait for an unevaluated tick; snapshot sums + identity
    //   EV_DIV1   : DIV_N restoring-division steps  (vwap = Σpv / Σv)
    //   EV_LATCH1 : capture quotient 1, load dividend 2
    //   EV_DIV2   : DIV_N steps                     (msq  = Σppv / Σv)
    //   EV_DECIDE : capture quotient 2; band test, edges, maybe fire
    //
    // Divide-by-zero guard: the wire format allows qty=0 frames, so sum_v
    // can be zero even after accepted ticks. sum_v==0 skips evaluation.
    //-------------------------------------------------------------------------
    typedef enum logic [2:0]
        { EV_IDLE, EV_DIV1, EV_LATCH1, EV_DIV2, EV_DECIDE } ev_state_t;
    ev_state_t ev;

    // snapshot
    logic [V_W-1:0]   s_den;
    logic [PPV_W-1:0] s_ppv;
    logic [31:0]      s_price;
    logic [47:0]      s_sym;
    logic [63:0]      s_hts, s_fts;
    logic             s_warm;

    // pending / coalescing latch
    logic        pend_v;
    logic [31:0] pend_price;
    logic [47:0] pend_sym;
    logic [63:0] pend_hts, pend_fts;

    // divider
    logic [DIV_N-1:0] div_q;
    logic [V_W:0]     div_r;
    logic [DIV_N-1:0] div_x;
    logic [CW-1:0]    div_i;

    logic [31:0] q_vwap;

    // edge state
    logic below_prev, above_prev, primed;

    // decision arithmetic (combinational; q_msq comes straight off div_q
    // in EV_DECIDE, q_vwap was registered in EV_LATCH1)
    logic [63:0] q_msq_c;
    logic [63:0] vwap_sq;
    logic [63:0] variance;
    logic [31:0] diff;
    logic [63:0] diff_sq;
    logic [71:0] thr_wide;
    logic        below_now, above_now;

    always_comb begin
        q_msq_c   = 64'(div_q);
        vwap_sq   = 64'(q_vwap) * 64'(q_vwap);
        variance  = (q_msq_c >= vwap_sq) ? (q_msq_c - vwap_sq) : 64'd0;
        diff      = (s_price < q_vwap) ? (q_vwap - s_price) : 32'd0;
        diff_sq   = 64'(diff) * 64'(diff);
        thr_wide  = 72'(variance) * 72'(K2_Q8);
        below_now = (s_price < q_vwap) && (diff_sq > 64'(thr_wide >> 8));
        above_now = (s_price >= q_vwap);
    end

    // one restoring-division step (shared by both divide states)
    logic [V_W:0] r_shift, r_sub;
    always_comb begin
        r_shift = { div_r[V_W-1:0], div_x[DIV_N-1] };
        r_sub   = r_shift - {1'b0, s_den};
    end

    always_ff @(posedge clk) begin
        if (!rst_n || clear) begin
            ev <= EV_IDLE;
            pend_v <= 1'b0;
            pend_price <= '0; pend_sym <= '0; pend_hts <= '0; pend_fts <= '0;
            s_den <= '0; s_ppv <= '0;
            s_price <= '0; s_sym <= '0; s_hts <= '0; s_fts <= '0;
            s_warm <= 1'b0;
            div_q <= '0; div_r <= '0; div_x <= '0; div_i <= '0;
            q_vwap <= '0;
            below_prev <= 1'b0; above_prev <= 1'b0; primed <= 1'b0;
            signal_valid <= 1'b0; signal_symbol <= '0; signal_side <= '0;
            signal_price <= '0; signal_host_ts <= '0; signal_fpga_ts <= '0;
            vwap_out <= '0;
            eval_skips <= '0;
        end else begin
            signal_valid <= 1'b0;

            // a completed tick requests evaluation; if one is already
            // waiting, the older request is coalesced (counted, and its
            // identity replaced by the newest — the next evaluation will
            // use the newest sums anyway)
            if (a3_done) begin
                if (pend_v)
                    eval_skips <= eval_skips + 1;
                pend_v     <= 1'b1;
                pend_price <= a3_price;
                pend_sym   <= a3_sym;
                pend_hts   <= a3_hts;
                pend_fts   <= a3_fts;
            end

            unique case (ev)
                EV_IDLE: begin
                    // note: reads pend_v as latched LAST cycle; the same-
                    // cycle a3_done path above lands in pend_* this edge
                    // and starts next cycle — one dead cycle, irrelevant
                    // against a 192-cycle evaluation.
                    if (pend_v) begin
                        if (sum_v != '0) begin
                            s_den   <= sum_v;
                            s_ppv   <= sum_ppv;
                            s_price <= pend_price;
                            s_sym   <= pend_sym;
                            s_hts   <= pend_hts;
                            s_fts   <= pend_fts;
                            s_warm  <= (sess_ticks >= 32'(WARMUP_N));
                            div_x   <= PPV_W'(sum_pv);   // dividend 1
                            div_q   <= '0;
                            div_r   <= '0;
                            div_i   <= '0;
                            ev      <= EV_DIV1;
                        end
                        pend_v <= 1'b0;      // consumed (or unevaluable)
                    end
                end

                EV_DIV1: begin
                    if (!r_sub[V_W]) begin
                        div_r <= r_sub;
                        div_q <= { div_q[DIV_N-2:0], 1'b1 };
                    end else begin
                        div_r <= r_shift;
                        div_q <= { div_q[DIV_N-2:0], 1'b0 };
                    end
                    div_x <= { div_x[DIV_N-2:0], 1'b0 };
                    if (div_i == CW'(DIV_N-1)) begin
                        div_i <= '0;
                        ev    <= EV_LATCH1;
                    end else
                        div_i <= div_i + 1'b1;
                end

                EV_LATCH1: begin
                    q_vwap <= 32'(div_q);    // vwap fits 32b for any sane
                                             // price (quotient ≤ max price)
                    div_x  <= s_ppv;         // dividend 2
                    div_q  <= '0;
                    div_r  <= '0;
                    div_i  <= '0;
                    ev     <= EV_DIV2;
                end

                EV_DIV2: begin
                    if (!r_sub[V_W]) begin
                        div_r <= r_sub;
                        div_q <= { div_q[DIV_N-2:0], 1'b1 };
                    end else begin
                        div_r <= r_shift;
                        div_q <= { div_q[DIV_N-2:0], 1'b0 };
                    end
                    div_x <= { div_x[DIV_N-2:0], 1'b0 };
                    if (div_i == CW'(DIV_N-1)) begin
                        div_i <= '0;
                        ev    <= EV_DECIDE;
                    end else
                        div_i <= div_i + 1'b1;
                end

                EV_DECIDE: begin
                    vwap_out <= q_vwap;
                    if (s_warm) begin
                        if (primed) begin
                            // SELL edge dominates on simultaneity — header
                            if (!above_prev && above_now) begin
                                signal_valid   <= 1'b1;
                                signal_side    <= SIDE_SELL;
                                signal_symbol  <= s_sym;
                                signal_price   <= s_price;
                                signal_host_ts <= s_hts;
                                signal_fpga_ts <= s_fts;
                            end else if (below_prev && !below_now) begin
                                signal_valid   <= 1'b1;
                                signal_side    <= SIDE_BUY;
                                signal_symbol  <= s_sym;
                                signal_price   <= s_price;
                                signal_host_ts <= s_hts;
                                signal_fpga_ts <= s_fts;
                            end
                        end
                        below_prev <= below_now;
                        above_prev <= above_now;
                        primed     <= 1'b1;    // first warm eval primes only
                    end
                    ev <= EV_IDLE;
                end

                default: ev <= EV_IDLE;
            endcase
        end
    end

endmodule

`default_nettype wire
