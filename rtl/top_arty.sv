//-----------------------------------------------------------------------------
// top_arty.sv — Arty A7-100T top level: tick parser layer + echo TX layer
//
// RX side (MANUAL.md §4 checklist — all items):
//  [x] uart_rx @ CLK_HZ=100_000_000, BAUD=115_200
//  [x] uart_rx byte stream -> tick_parser
//  [x] Host->FPGA UART pin -> uart_rx.rx
//      *** PIN NAME NOTE: on the Arty the host->FPGA line is `uart_txd_in`
//      *** (A9) — named from the host FTDI's perspective. `uart_rxd_out`
//      *** (D10) is FPGA->host, now used by the TX layer below.
//  [x] Decoded tick re-registered onto the tick_* bus (indicator boundary,
//      mark_debug for ILA)
//  [x] parse_error -> LED0 (stretched) + 16-bit saturating counter
//  [x] 64-bit us counter latched on SOF (timestamp_us.sv)
//
// TX side (new layer):
//  [x] Every good tick's record (incl. FPGA arrival timestamp) is written
//      into a sync FIFO on tick_valid
//  [x] frame_tx pops records and serializes 30-byte echo frames (see
//      frame_tx.sv header for the FPGA->host wire format)
//  [x] uart_tx drives uart_rxd_out (D10) back to the host
//  [x] If the FIFO is full when a tick lands (sustained back-to-back input:
//      30-byte echoes can't keep up with 22-byte input), the tick is DROPPED
//      and counted in tx_drop_count — decoded data is never corrupted, and
//      drops are observable. Invariant: echoes_sent + drops == ticks decoded.
//
// LED map:
//   led[0] = parse_error occurred (stretched ~0.34 s)
//   led[1] = msg_valid activity (stretched)
//   led[2] = UART framing error (stretched)
//   led[3] = heartbeat (~0.75 Hz)
//-----------------------------------------------------------------------------
`timescale 1ns / 1ps
`default_nettype none

module top_arty #(
    parameter int          CLK_HZ         = 100_000_000,
    parameter int          BAUD           = 921_600,   // was 115_200;
                                                       // 100MHz/921600 rounds
                                                       // to 108 clks/bit,
                                                       // actual ~925.9kHz
                                                       // (+0.469%) — well
                                                       // within normal UART
                                                       // tolerance, same
                                                       // category of effect
                                                       // as the sim BAUD
                                                       // speedup trick
    parameter int          TX_FIFO_DEPTH  = 16,      // echo FIFO; power of 2
    parameter int          SIG_FIFO_DEPTH = 4,       // signal FIFO; power of 2
    parameter logic [47:0] DEFAULT_SYM0   = "SPY   ", // slot 0 reset value
    parameter int          FAST_N         = 8,       // fast SMA window (pow 2)
    parameter int          SLOW_N         = 32,      // slow SMA window (pow 2)
    parameter int          EMA_KF         = 3,       // fast EMA: alpha 2^-KF
    parameter int          EMA_KS         = 5,       // slow EMA: alpha 2^-KS
    parameter int          EMA_WARMUP     = 32       // = SLOW_N: fair start
)(
    input  wire  logic       clk100,        // 100 MHz board oscillator (E3)
    input  wire  logic       ck_rst,        // red RESET button, ACTIVE LOW (C2)
    input  wire  logic       uart_txd_in,   // host -> FPGA UART data (A9)
    output logic       uart_rxd_out,  // FPGA -> host UART data (D10)
    output logic [3:0] led            // LD4..LD7
);

    //-------------------------------------------------------------------------
    // Reset conditioning: 2-FF sync + 16-cycle hold-off (walkthrough §6.1)
    //-------------------------------------------------------------------------
    logic       rst_n;
    logic       rst_meta, rst_sync;
    logic [3:0] rst_cnt;

    always_ff @(posedge clk100) begin
        rst_meta <= ck_rst;
        rst_sync <= rst_meta;
        if (!rst_sync) begin
            rst_cnt <= '0;
            rst_n   <= 1'b0;
        end else if (rst_cnt != 4'hF) begin
            rst_cnt <= rst_cnt + 1'b1;
            rst_n   <= 1'b0;
        end else begin
            rst_n   <= 1'b1;
        end
    end

    //-------------------------------------------------------------------------
    // RX chain: UART -> parser -> timestamp
    //-------------------------------------------------------------------------
    logic [7:0] rx_data;
    logic       rx_valid;
    logic       rx_error;

    uart_rx #(
        .CLK_HZ ( CLK_HZ ),
        .BAUD   ( BAUD   )
    ) u_uart_rx (
        .clk      ( clk100      ),
        .rst_n    ( rst_n       ),
        .rx       ( uart_txd_in ),
        .rx_data  ( rx_data     ),
        .rx_valid ( rx_valid    ),
        .rx_error ( rx_error    )
    );

    logic [7:0]  msg_type;
    logic [47:0] symbol;
    logic [31:0] price;
    logic [15:0] qty;
    logic [7:0]  side;
    logic [63:0] host_tstamp;
    logic        msg_valid;
    logic        parse_error;
    logic        sof_seen;

    tick_parser u_tick_parser (
        .clk         ( clk100      ),
        .rst_n       ( rst_n       ),
        .rx_data     ( rx_data     ),
        .rx_valid    ( rx_valid    ),
        .msg_type    ( msg_type    ),
        .symbol      ( symbol      ),
        .price       ( price       ),
        .qty         ( qty         ),
        .side        ( side        ),
        .host_tstamp ( host_tstamp ),
        .msg_valid   ( msg_valid   ),
        .parse_error ( parse_error ),
        .sof_seen    ( sof_seen    )
    );

    logic [63:0] us_now;
    logic [63:0] pending_us;
    logic [63:0] arrival_us;

    timestamp_us #(
        .CLK_HZ ( CLK_HZ )
    ) u_timestamp (
        .clk        ( clk100     ),
        .rst_n      ( rst_n      ),
        .sof_seen   ( sof_seen   ),
        .msg_valid  ( msg_valid  ),
        .us_now     ( us_now     ),
        .pending_us ( pending_us ),
        .arrival_us ( arrival_us )
    );

    //-------------------------------------------------------------------------
    // Indicator-engine boundary: registered tick_* bus (walkthrough §6.3).
    // The TX FIFO below also feeds from this bus, so the echo frame and the
    // (future) indicator engine see byte-identical data.
    //-------------------------------------------------------------------------
    (* mark_debug = "true" *) logic        tick_valid;
    (* mark_debug = "true" *) logic [7:0]  tick_type;
    (* mark_debug = "true" *) logic [47:0] tick_symbol;
    (* mark_debug = "true" *) logic [31:0] tick_price;
    (* mark_debug = "true" *) logic [15:0] tick_qty;
    (* mark_debug = "true" *) logic [7:0]  tick_side;
    (* mark_debug = "true" *) logic [63:0] tick_host_ts;
    (* mark_debug = "true" *) logic [63:0] tick_fpga_ts;

    always_ff @(posedge clk100) begin
        if (!rst_n) begin
            tick_valid   <= 1'b0;
            tick_type    <= '0;
            tick_symbol  <= '0;
            tick_price   <= '0;
            tick_qty     <= '0;
            tick_side    <= '0;
            tick_host_ts <= '0;
            tick_fpga_ts <= '0;
        end else begin
            tick_valid <= msg_valid;
            if (msg_valid) begin
                tick_type    <= msg_type;
                tick_symbol  <= symbol;
                tick_price   <= price;
                tick_qty     <= qty;
                tick_side    <= side;
                tick_host_ts <= host_tstamp;
                // pending_us, NOT arrival_us: arrival_us commits on this
                // same edge, so reading it here races one cycle behind.
                // pending_us was latched at this frame's SOF and is stable.
                tick_fpga_ts <= pending_us;
            end
        end
    end

    //-------------------------------------------------------------------------
    // v2.0: runtime symbol register file (8 slots, written by TYPE 0x10
    // frames — see symcfg.sv) feeding 8 parallel instances of EACH engine.
    // One tick carries one symbol, so at most one slot per strategy fires
    // per tick (host enforces slot uniqueness); a lowest-slot priority
    // encoder folds the 8 lanes into one record per strategy.
    //-------------------------------------------------------------------------
    logic [7:0]   slot_valid;
    logic [7:0]   slot_wr;
    logic [383:0] slots_flat;      // slot g at [g*48 +: 48] — see symcfg.sv

    symcfg #( .DEFAULT_SYM0 ( DEFAULT_SYM0 ) ) u_symcfg (
        .clk        ( clk100      ),
        .rst_n      ( rst_n       ),
        .tick_valid ( tick_valid  ),
        .msg_type   ( tick_type   ),
        .symbol     ( tick_symbol ),
        .qty        ( tick_qty    ),
        .side       ( tick_side   ),
        .slot_valid ( slot_valid  ),
        .slot_wr    ( slot_wr     ),
        .slots_flat ( slots_flat  )
    );

    logic [7:0]  sma_v_a, ema_v_a;
    logic [47:0] sma_sym_a [0:7];   logic [47:0] ema_sym_a [0:7];
    logic [7:0]  sma_side_a [0:7];  logic [7:0]  ema_side_a [0:7];
    logic [31:0] sma_price_a [0:7]; logic [31:0] ema_price_a [0:7];
    logic [63:0] sma_fts_a [0:7];   logic [63:0] ema_fts_a [0:7];
    logic [31:0] sma_f_a [0:7], sma_s_a [0:7];
    logic [31:0] ema_f_a [0:7], ema_s_a [0:7];

    generate
        for (genvar g = 0; g < 8; g++) begin : g_engines
            indicator_engine #(
                .FAST_N ( FAST_N ), .SLOW_N ( SLOW_N )
            ) u_sma (
                .clk(clk100), .rst_n(rst_n),
                .target_symbol ( slots_flat[g*48 +: 48] ),
                .slot_en       ( slot_valid[g] ),
                .state_rst     ( slot_wr[g]    ),
                .tick_valid(tick_valid), .msg_type(tick_type),
                .symbol(tick_symbol), .price(tick_price),
                .host_ts(tick_host_ts), .fpga_ts(tick_fpga_ts),
                .signal_valid(sma_v_a[g]), .signal_symbol(sma_sym_a[g]),
                .signal_side(sma_side_a[g]), .signal_price(sma_price_a[g]),
                .signal_host_ts(), .signal_fpga_ts(sma_fts_a[g]),
                .sma_fast(sma_f_a[g]), .sma_slow(sma_s_a[g]),
                .smas_valid()
            );
            ema_engine #(
                .K_FAST ( EMA_KF ), .K_SLOW ( EMA_KS ),
                .WARMUP_N ( EMA_WARMUP )
            ) u_ema (
                .clk(clk100), .rst_n(rst_n),
                .target_symbol ( slots_flat[g*48 +: 48] ),
                .slot_en       ( slot_valid[g] ),
                .state_rst     ( slot_wr[g]    ),
                .tick_valid(tick_valid), .msg_type(tick_type),
                .symbol(tick_symbol), .price(tick_price),
                .host_ts(tick_host_ts), .fpga_ts(tick_fpga_ts),
                .signal_valid(ema_v_a[g]), .signal_symbol(ema_sym_a[g]),
                .signal_side(ema_side_a[g]), .signal_price(ema_price_a[g]),
                .signal_host_ts(), .signal_fpga_ts(ema_fts_a[g]),
                .ema_fast(ema_f_a[g]), .ema_slow(ema_s_a[g]),
                .emas_valid()
            );
        end
    endgenerate

    //-------------------------------------------------------------------------
    // Per-strategy lowest-slot priority encoder -> one record per strategy,
    // then the existing SMA-vs-EMA same-cycle pend arbiter (unchanged logic,
    // wider records: REC_W = 8+48+32+16+8+64+64 = 240).
    //-------------------------------------------------------------------------
    localparam int REC_W = 240;

    logic             signal_valid, ema_sig_valid;
    logic [REC_W-1:0] sma_rec, ema_rec;

    always_comb begin
        signal_valid = |sma_v_a;
        sma_rec = '0;
        for (int i = 7; i >= 0; i--)
            if (sma_v_a[i])
                sma_rec = { 8'h03, sma_sym_a[i], sma_price_a[i],
                            16'h0000, sma_side_a[i],
                            sma_f_a[i], sma_s_a[i], sma_fts_a[i] };
        ema_sig_valid = |ema_v_a;
        ema_rec = '0;
        for (int i = 7; i >= 0; i--)
            if (ema_v_a[i])
                ema_rec = { 8'h04, ema_sym_a[i], ema_price_a[i],
                            16'h0000, ema_side_a[i],
                            ema_f_a[i], ema_s_a[i], ema_fts_a[i] };
    end

    logic [REC_W-1:0] sig_rd_data;
    logic             sig_full, sig_empty, sig_rd_en;

    logic             pend_v;
    logic [REC_W-1:0] pend_d;
    logic             sig_wr_en;
    logic [REC_W-1:0] sig_wr_data;

    always_comb begin
        if (signal_valid)        begin sig_wr_en = 1'b1; sig_wr_data = sma_rec; end
        else if (ema_sig_valid)  begin sig_wr_en = 1'b1; sig_wr_data = ema_rec; end
        else if (pend_v)         begin sig_wr_en = 1'b1; sig_wr_data = pend_d;  end
        else                     begin sig_wr_en = 1'b0; sig_wr_data = '0;      end
    end

    always_ff @(posedge clk100) begin
        if (!rst_n) begin
            pend_v <= 1'b0;
            pend_d <= '0;
        end else begin
            if (signal_valid && ema_sig_valid) begin
                pend_v <= 1'b1;
                pend_d <= ema_rec;
            end else if (pend_v && !signal_valid && !ema_sig_valid) begin
                pend_v <= 1'b0;
            end
        end
    end

    //-------------------------------------------------------------------------
    // Echo FIFO (LO priority): every decoded frame — trades, quotes, and
    // symcfg commands — is echoed to the host. A symcfg echo (wire 0x90)
    // is the host's configuration ACK: it reads back the slot index and
    // symbol the fabric actually latched.
    //-------------------------------------------------------------------------
    logic [REC_W-1:0] fifo_wr_data, fifo_rd_data;
    logic             fifo_full, fifo_empty, fifo_rd_en;

    assign fifo_wr_data = { tick_type, tick_symbol, tick_price,
                            tick_qty, tick_side, tick_host_ts, tick_fpga_ts };

    sync_fifo #(
        .WIDTH ( REC_W         ),
        .DEPTH ( TX_FIFO_DEPTH )
    ) u_tx_fifo (
        .clk     ( clk100       ),
        .rst_n   ( rst_n        ),
        .wr_en   ( tick_valid   ),
        .wr_data ( fifo_wr_data ),
        .full    ( fifo_full    ),
        .rd_data ( fifo_rd_data ),
        .rd_en   ( fifo_rd_en   ),
        .empty   ( fifo_empty   )
    );

    (* mark_debug = "true" *) logic [15:0] tx_drop_count;

    always_ff @(posedge clk100) begin
        if (!rst_n)
            tx_drop_count <= '0;
        else if (tick_valid && fifo_full && tx_drop_count != 16'hFFFF)
            tx_drop_count <= tx_drop_count + 1'b1;
    end

    sync_fifo #(
        .WIDTH ( REC_W          ),
        .DEPTH ( SIG_FIFO_DEPTH )
    ) u_sig_fifo (
        .clk     ( clk100      ),
        .rst_n   ( rst_n       ),
        .wr_en   ( sig_wr_en   ),
        .wr_data ( sig_wr_data ),
        .full    ( sig_full    ),
        .rd_data ( sig_rd_data ),
        .rd_en   ( sig_rd_en   ),
        .empty   ( sig_empty   )
    );

    (* mark_debug = "true" *) logic [15:0] sig_drop_count;

    always_ff @(posedge clk100) begin
        if (!rst_n)
            sig_drop_count <= '0;
        else if (sig_wr_en && sig_full && sig_drop_count != 16'hFFFF)
            sig_drop_count <= sig_drop_count + 1'b1;
    end

    logic [7:0] tx_data;
    logic       tx_valid, tx_ready;

    frame_tx u_frame_tx (
        .clk        ( clk100       ),
        .rst_n      ( rst_n        ),
        .hi_empty   ( sig_empty    ),
        .hi_rd_data ( sig_rd_data  ),
        .hi_rd_en   ( sig_rd_en    ),
        .lo_empty   ( fifo_empty   ),
        .lo_rd_data ( fifo_rd_data ),
        .lo_rd_en   ( fifo_rd_en   ),
        .tx_data    ( tx_data      ),
        .tx_valid   ( tx_valid     ),
        .tx_ready   ( tx_ready     )
    );

    uart_tx #(
        .CLK_HZ ( CLK_HZ ),
        .BAUD   ( BAUD   )
    ) u_uart_tx (
        .clk      ( clk100       ),
        .rst_n    ( rst_n        ),
        .tx_data  ( tx_data      ),
        .tx_valid ( tx_valid     ),
        .tx_ready ( tx_ready     ),
        .tx       ( uart_rxd_out )
    );

    //-------------------------------------------------------------------------
    // Error counter + LED stretchers + heartbeat (walkthrough §6.4)
    //-------------------------------------------------------------------------
    (* mark_debug = "true" *) logic [15:0] parse_error_count;

    always_ff @(posedge clk100) begin
        if (!rst_n)
            parse_error_count <= '0;
        else if (parse_error && parse_error_count != 16'hFFFF)
            parse_error_count <= parse_error_count + 1'b1;
    end

    localparam int STRETCH_W = 25;
    logic [STRETCH_W-1:0] str_perr, str_msg, str_uerr;

    always_ff @(posedge clk100) begin
        if (!rst_n) begin
            str_perr <= '0;
            str_msg  <= '0;
            str_uerr <= '0;
        end else begin
            str_perr <= parse_error ? '1 : (str_perr != 0 ? str_perr - 1'b1 : '0);
            str_msg  <= msg_valid   ? '1 : (str_msg  != 0 ? str_msg  - 1'b1 : '0);
            str_uerr <= rx_error    ? '1 : (str_uerr != 0 ? str_uerr - 1'b1 : '0);
        end
    end

    logic [26:0] hb_cnt;
    always_ff @(posedge clk100) begin
        if (!rst_n) hb_cnt <= '0;
        else        hb_cnt <= hb_cnt + 1'b1;
    end

    assign led[0] = (str_perr != 0);
    assign led[1] = (str_msg  != 0);
    assign led[2] = (str_uerr != 0);
    assign led[3] = hb_cnt[26];

endmodule

`default_nettype wire
